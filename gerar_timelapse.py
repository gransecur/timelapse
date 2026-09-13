#!/usr/bin/env python3
"""
Gerador de time-lapse de obras a partir de fotos no Google Drive.

Lê as configurações de variáveis de ambiente (preenchidas pelo GitHub Actions),
baixa as fotos, monta o vídeo em 4K com FFmpeg e envia o resultado de volta
para o Google Drive.

O processamento é feito em lotes: baixa um pedaço, codifica um trecho do vídeo,
apaga os arquivos e segue para o próximo. Isso mantém o uso de disco baixo
independente do tamanho da obra.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# ─────────────────────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────────────────────

FPS = 30                      # fixo — padrão fluido para time-lapse
FRAMES_POR_LOTE = 600         # quantos frames processar por vez (controla o disco)
DOWNLOADS_SIMULTANEOS = 16    # quantas fotos baixar em paralelo
ESCOPOS = ['https://www.googleapis.com/auth/drive']
PADRAO_DATA = re.compile(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})')
EXTENSOES = ('.jpg', '.jpeg', '.png', '.webp')

PRESETS = {
    'rapido': 'veryfast',
    'equilibrado': 'medium',
    'qualidade': 'slow',
}


def env(nome, padrao=''):
    return os.environ.get(nome, padrao).strip()


def log(msg):
    print(msg, flush=True)


def secao(titulo):
    log('')
    log('─' * 60)
    log(f'  {titulo}')
    log('─' * 60)


def normalizar(texto):
    """Remove acentos e deixa minúsculo, para comparar nomes de pasta."""
    d = unicodedata.normalize('NFKD', texto)
    return ''.join(c for c in d if not unicodedata.combining(c)).lower().strip()


def fmt_duracao(segundos):
    return f'{int(segundos // 60)}m {int(round(segundos % 60)):02d}s'


def apelido(texto, limite=40):
    """Transforma o nome da pasta em algo seguro para nome de arquivo."""
    sem_acento = normalizar(texto).upper()
    limpo = re.sub(r'[^A-Z0-9]+', '-', sem_acento).strip('-')
    return (limpo[:limite].rstrip('-')) or 'OBRA'


# ─────────────────────────────────────────────────────────────
# Google Drive
# ─────────────────────────────────────────────────────────────

def conectar_drive():
    """Autentica usando a conta de serviço guardada no secret do GitHub."""
    bruto = os.environ.get('GDRIVE_SERVICE_ACCOUNT', '')
    if not bruto:
        sys.exit('❌ Secret GDRIVE_SERVICE_ACCOUNT não encontrado no repositório.')
    try:
        info = json.loads(bruto)
    except json.JSONDecodeError:
        sys.exit('❌ O secret GDRIVE_SERVICE_ACCOUNT não contém um JSON válido.')

    cred = service_account.Credentials.from_service_account_info(info, scopes=ESCOPOS)
    log(f'🔑 Conta de serviço: {info.get("client_email", "?")}')
    return build('drive', 'v3', credentials=cred, cache_discovery=False)


def parece_id(texto):
    """IDs do Drive são longos e sem espaços."""
    return len(texto) > 20 and ' ' not in texto and '/' not in texto


def resolver_pasta(drive, referencia, rotulo):
    """Aceita o ID da pasta ou o nome dela (busca entre as compartilhadas)."""
    if parece_id(referencia):
        try:
            info = drive.files().get(
                fileId=referencia, fields='id,name',
                supportsAllDrives=True).execute()
            log(f'📂 {rotulo}: {info["name"]}')
            return info['id'], info['name']
        except HttpError as e:
            sys.exit(f'❌ Não consegui abrir a pasta {referencia}.\n'
                     f'   Confirme se ela foi compartilhada com a conta de serviço.\n   {e}')

    alvo = normalizar(referencia)
    resp = drive.files().list(
        q="mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields='files(id,name)', pageSize=200,
        includeItemsFromAllDrives=True, supportsAllDrives=True).execute()

    candidatos = [f for f in resp.get('files', []) if alvo in normalizar(f['name'])]
    if not candidatos:
        disponiveis = ', '.join(f['name'] for f in resp.get('files', [])) or '(nenhuma)'
        sys.exit(f'❌ Nenhuma pasta chamada "{referencia}" foi compartilhada com a conta de serviço.\n'
                 f'   Pastas visíveis: {disponiveis}')
    if len(candidatos) > 1:
        nomes = ', '.join(f'{c["name"]} ({c["id"]})' for c in candidatos)
        log(f'⚠️  Mais de uma pasta corresponde a "{referencia}": {nomes}')
        log(f'   Usando a primeira. Informe o ID para escolher outra.')

    log(f'📂 {rotulo}: {candidatos[0]["name"]}')
    return candidatos[0]['id'], candidatos[0]['name']


def listar_fotos(drive, pasta_id):
    """Percorre a pasta e todas as subpastas (Ano > Mês > Dia) buscando imagens."""
    fotos = []
    fila = [pasta_id]
    pastas_lidas = 0

    while fila:
        atual = fila.pop(0)
        pastas_lidas += 1
        token = None
        while True:
            resp = drive.files().list(
                q=f"'{atual}' in parents and trashed=false",
                fields='nextPageToken, files(id,name,mimeType)',
                pageSize=1000, pageToken=token,
                includeItemsFromAllDrives=True, supportsAllDrives=True).execute()

            for item in resp.get('files', []):
                if item['mimeType'] == 'application/vnd.google-apps.folder':
                    fila.append(item['id'])
                elif item['name'].lower().endswith(EXTENSOES):
                    m = PADRAO_DATA.search(item['name'])
                    if not m:
                        continue
                    y, mo, d, h, mi, s = m.groups()
                    try:
                        quando = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
                    except ValueError:
                        continue
                    fotos.append({'id': item['id'], 'nome': item['name'], 'quando': quando})

            token = resp.get('nextPageToken')
            if not token:
                break

        if pastas_lidas % 25 == 0:
            log(f'   ... {pastas_lidas} pastas lidas, {len(fotos)} fotos encontradas')

    fotos.sort(key=lambda f: f['quando'])
    log(f'   {pastas_lidas} pastas lidas, {len(fotos)} fotos com data no nome')
    return fotos


def baixar_foto(drive, foto_id, destino):
    """Baixa uma foto. Tenta de novo em caso de falha temporária."""
    for tentativa in range(3):
        try:
            pedido = drive.files().get_media(fileId=foto_id, supportsAllDrives=True)
            with io.FileIO(destino, 'wb') as arquivo:
                baixador = MediaIoBaseDownload(arquivo, pedido, chunksize=5 * 1024 * 1024)
                concluido = False
                while not concluido:
                    _status, concluido = baixador.next_chunk()
            return True
        except (HttpError, OSError):
            if tentativa < 2:
                time.sleep(1 + tentativa * 2)
            else:
                return False
    return False


def enviar_para_drive(drive, caminho, pasta_destino_id, nome):
    """Envia o vídeo pronto e devolve o link de visualização."""
    midia = MediaFileUpload(caminho, mimetype='video/mp4', resumable=True, chunksize=16 * 1024 * 1024)
    corpo = {'name': nome, 'parents': [pasta_destino_id]}

    pedido = drive.files().create(
        body=corpo, media_body=midia,
        fields='id,webViewLink', supportsAllDrives=True)

    resposta = None
    ultimo_pct = -10
    while resposta is None:
        status, resposta = pedido.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct - ultimo_pct >= 10:
                ultimo_pct = pct
                log(f'   enviando... {pct}%')
    return resposta


# ─────────────────────────────────────────────────────────────
# Seleção das fotos
# ─────────────────────────────────────────────────────────────

def selecionar(fotos, cfg):
    """Escolhe quais fotos entram no vídeo, dando o mesmo tempo de tela a cada dia."""
    d_ini = datetime.strptime(cfg['data_inicio'], '%Y-%m-%d').date()
    d_fim = datetime.strptime(cfg['data_fim'], '%Y-%m-%d').date()
    h_ini = datetime.strptime(cfg['hora_inicial'], '%H:%M').time()
    h_fim = datetime.strptime(cfg['hora_final'], '%H:%M').time()

    no_periodo = [f for f in fotos if d_ini <= f['quando'].date() <= d_fim]
    if not no_periodo:
        sys.exit('❌ Nenhuma foto no período escolhido.')

    diurnas = [f for f in no_periodo if h_ini <= f['quando'].time() <= h_fim]
    noturnas = len(no_periodo) - len(diurnas)
    if not diurnas:
        sys.exit('❌ Nenhuma foto dentro do horário escolhido.')

    por_dia = defaultdict(list)
    for f in diurnas:
        por_dia[f['quando'].date()].append(f)

    fracos = [d for d in por_dia if len(por_dia[d]) < cfg['min_fotos_dia']]
    for d in fracos:
        del por_dia[d]
    dias = sorted(por_dia)
    if not dias:
        sys.exit(f'❌ Nenhum dia tem ao menos {cfg["min_fotos_dia"]} fotos. Reduza esse mínimo.')

    total_frames = max(1, round(cfg['duracao_segundos'] * FPS))

    # Reparte os frames igualmente entre os dias
    ideal = total_frames / len(dias)
    cotas, sobra = [], 0.0
    for _ in dias:
        sobra += ideal
        n = int(round(sobra))
        sobra -= n
        cotas.append(max(0, n))

    diferenca = total_frames - sum(cotas)
    i = 0
    while diferenca != 0 and cotas:
        j = i % len(cotas)
        if diferenca > 0:
            cotas[j] += 1
            diferenca -= 1
        elif cotas[j] > 0:
            cotas[j] -= 1
            diferenca += 1
        i += 1

    # Dentro de cada dia, pega as fotos espalhadas ao longo do horário
    escolhidas = []
    for dia, n in zip(dias, cotas):
        if n == 0:
            continue
        do_dia = sorted(por_dia[dia], key=lambda f: f['quando'])
        if n == 1:
            escolhidas.append(do_dia[len(do_dia) // 2])
        else:
            passo = (len(do_dia) - 1) / (n - 1)
            escolhidas.extend(do_dia[round(k * passo)] for k in range(n))

    dias_usados = sum(1 for c in cotas if c > 0)
    unicas = len({f['id'] for f in escolhidas})

    secao('RESUMO DA SELEÇÃO')
    log(f'  Duração do vídeo ........... {fmt_duracao(len(escolhidas) / FPS)}')
    log(f'  Frames ..................... {len(escolhidas)} a {FPS}fps')
    log(f'  Dias no vídeo .............. {dias_usados} de {len(dias) + len(fracos)}')
    log(f'  Fotos diferentes ........... {unicas}')
    log(f'  Fotos noturnas descartadas . {noturnas}')
    log(f'  Dias com poucas fotos ...... {len(fracos)} (menos de {cfg["min_fotos_dia"]})')
    log(f'  Primeira foto .............. {escolhidas[0]["quando"]:%d/%m/%Y %H:%M}')
    log(f'  Última foto ................ {escolhidas[-1]["quando"]:%d/%m/%Y %H:%M}')

    if unicas < len(escolhidas) * 0.7:
        log('')
        log('  ⚠️  Muitas fotos serão repetidas para preencher a duração pedida.')
        log(f'     Para um vídeo mais fluido, use algo perto de {fmt_duracao(unicas / FPS)}.')

    return escolhidas


# ─────────────────────────────────────────────────────────────
# Montagem do vídeo
# ─────────────────────────────────────────────────────────────

def codificar_lote(pasta_seq, saida, cfg):
    """Codifica um trecho do vídeo a partir de uma sequência numerada de imagens."""
    larg, alt = cfg['largura'], cfg['altura']
    filtro = (f'scale={larg}:{alt}:force_original_aspect_ratio=decrease,'
              f'pad={larg}:{alt}:(ow-iw)/2:(oh-ih)/2:black,setsar=1')

    cmd = [
        'ffmpeg', '-y', '-loglevel', 'error',
        '-framerate', str(FPS),
        '-i', os.path.join(pasta_seq, '%06d.jpg'),
        '-vf', filtro,
        '-c:v', 'libx264', '-preset', cfg['preset'], '-crf', str(cfg['qualidade']),
        '-pix_fmt', 'yuv420p', '-r', str(FPS),
        saida,
    ]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        log(resultado.stderr[-2000:])
        sys.exit('❌ O FFmpeg falhou ao codificar um trecho do vídeo.')


def montar_video(drive, escolhidas, cfg, trabalho):
    """Baixa e codifica em lotes, depois junta tudo num arquivo só."""
    pasta_trechos = os.path.join(trabalho, 'trechos')
    os.makedirs(pasta_trechos, exist_ok=True)

    total = len(escolhidas)
    trechos = []
    inicio = time.time()

    for comeco in range(0, total, FRAMES_POR_LOTE):
        lote = escolhidas[comeco:comeco + FRAMES_POR_LOTE]
        numero = len(trechos) + 1

        pasta_fotos = os.path.join(trabalho, 'fotos')
        pasta_seq = os.path.join(trabalho, 'seq')
        shutil.rmtree(pasta_fotos, ignore_errors=True)
        shutil.rmtree(pasta_seq, ignore_errors=True)
        os.makedirs(pasta_fotos, exist_ok=True)
        os.makedirs(pasta_seq, exist_ok=True)

        # Baixa só as fotos diferentes deste lote, em paralelo
        unicas = sorted({f['id'] for f in lote})
        destinos = {fid: os.path.join(pasta_fotos, f'{fid}.jpg') for fid in unicas}

        pct = 100 * comeco / total
        log(f'⬇️  [{pct:5.1f}%] Lote {numero}: baixando {len(unicas)} fotos...')

        with ThreadPoolExecutor(max_workers=DOWNLOADS_SIMULTANEOS) as pool:
            ok = list(pool.map(lambda fid: baixar_foto(drive, fid, destinos[fid]), unicas))

        falhas = ok.count(False)
        if falhas:
            log(f'   ⚠️  {falhas} foto(s) não baixaram e serão puladas')

        # Monta a sequência numerada (fotos repetidas viram links, sem gastar disco)
        indice = 0
        for foto in lote:
            origem = destinos[foto['id']]
            if not os.path.exists(origem) or os.path.getsize(origem) == 0:
                continue
            indice += 1
            try:
                os.link(origem, os.path.join(pasta_seq, f'{indice:06d}.jpg'))
            except OSError:
                shutil.copy2(origem, os.path.join(pasta_seq, f'{indice:06d}.jpg'))

        if indice == 0:
            log('   ⚠️  Lote sem fotos válidas, pulando')
            continue

        trecho = os.path.join(pasta_trechos, f'trecho_{numero:04d}.mp4')
        log(f'🎬 [{pct:5.1f}%] Lote {numero}: codificando {indice} frames...')
        codificar_lote(pasta_seq, trecho, cfg)
        trechos.append(trecho)

        shutil.rmtree(pasta_fotos, ignore_errors=True)
        shutil.rmtree(pasta_seq, ignore_errors=True)

    if not trechos:
        sys.exit('❌ Nenhum trecho foi gerado.')

    # Junta os trechos sem recodificar (rápido e sem perda)
    log('')
    log(f'🔗 Juntando {len(trechos)} trecho(s)...')
    lista = os.path.join(trabalho, 'lista.txt')
    with open(lista, 'w') as f:
        for t in trechos:
            f.write(f"file '{t}'\n")

    final = os.path.join(trabalho, cfg['nome_arquivo'])
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
           '-i', lista, '-c', 'copy', '-movflags', '+faststart', final]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        log(resultado.stderr[-2000:])
        sys.exit('❌ Falha ao juntar os trechos.')

    shutil.rmtree(pasta_trechos, ignore_errors=True)
    log(f'   Concluído em {fmt_duracao(time.time() - inicio)}')
    return final


# ─────────────────────────────────────────────────────────────
# Principal
# ─────────────────────────────────────────────────────────────

def ler_configuracao():
    duracao_txt = env('DURACAO', '1:30')
    try:
        partes = [int(p) for p in duracao_txt.replace('.', ':').split(':')]
        duracao_segundos = partes[0] * 60 + partes[1] if len(partes) == 2 else partes[0]
    except (ValueError, IndexError):
        sys.exit(f'❌ Duração inválida: "{duracao_txt}". Use o formato minutos:segundos, ex: 1:30')

    if duracao_segundos < 1:
        sys.exit('❌ A duração precisa ser de pelo menos 1 segundo.')

    resolucao = env('RESOLUCAO', '3840x2160')
    try:
        largura, altura = (int(x) for x in resolucao.lower().split('x'))
    except ValueError:
        sys.exit(f'❌ Resolução inválida: "{resolucao}". Use por exemplo 3840x2160.')

    velocidade = normalizar(env('VELOCIDADE', 'rapido'))
    preset = PRESETS.get(velocidade, 'veryfast')

    return {
        'pasta_obra': env('PASTA_OBRA'),
        'pasta_destino': env('PASTA_DESTINO'),
        'data_inicio': env('DATA_INICIO', '2020-01-01'),
        'data_fim': env('DATA_FIM') or datetime.now().strftime('%Y-%m-%d'),
        'hora_inicial': env('HORA_INICIAL', '07:00'),
        'hora_final': env('HORA_FINAL', '17:00'),
        'min_fotos_dia': max(1, int(env('MIN_FOTOS_DIA', '3') or 3)),
        'duracao_segundos': duracao_segundos,
        'largura': largura,
        'altura': altura,
        'qualidade': max(14, min(30, int(env('QUALIDADE', '18') or 18))),
        'preset': preset,
    }


def main():
    inicio_geral = time.time()
    cfg = ler_configuracao()

    if not cfg['pasta_obra']:
        sys.exit('❌ Informe a pasta da obra (PASTA_OBRA).')

    secao('CONFIGURAÇÃO')
    log(f'  Período .......... {cfg["data_inicio"]} até {cfg["data_fim"]}')
    log(f'  Horário útil ..... {cfg["hora_inicial"]} às {cfg["hora_final"]}')
    log(f'  Duração alvo ..... {fmt_duracao(cfg["duracao_segundos"])}')
    log(f'  Resolução ........ {cfg["largura"]}x{cfg["altura"]}')
    log(f'  Qualidade ........ CRF {cfg["qualidade"]} (preset {cfg["preset"]})')
    log(f'  Mínimo por dia ... {cfg["min_fotos_dia"]} fotos')

    drive = conectar_drive()
    pasta_obra_id, nome_obra = resolver_pasta(drive, cfg['pasta_obra'], 'Pasta das fotos')
    if cfg['pasta_destino']:
        pasta_destino_id, _ = resolver_pasta(drive, cfg['pasta_destino'], 'Pasta de destino')
    else:
        pasta_destino_id = pasta_obra_id

    secao('LENDO O GOOGLE DRIVE')
    fotos = listar_fotos(drive, pasta_obra_id)
    if not fotos:
        sys.exit('❌ Nenhuma foto com data no nome foi encontrada.\n'
                 '   Esperado algo como: Obra HE_00_20250303024931.jpg')

    escolhidas = selecionar(fotos, cfg)

    # Carimbo de geração garante que cada vídeo tenha nome próprio
    carimbo = datetime.now().strftime('%Y%m%d-%H%M')
    rotulo_res = {3840: '4K', 2560: '1440p', 1920: '1080p'}.get(cfg['largura'],
                                                                f'{cfg["largura"]}p')
    cfg['nome_arquivo'] = (f'timelapse_{apelido(nome_obra)}'
                           f'_{cfg["data_inicio"]}_a_{cfg["data_fim"]}'
                           f'_{rotulo_res}_{carimbo}.mp4')
    log(f'📄 Nome do arquivo: {cfg["nome_arquivo"]}')

    secao('MONTANDO O VÍDEO')
    trabalho = tempfile.mkdtemp(prefix='timelapse_')
    try:
        final = montar_video(drive, escolhidas, cfg, trabalho)
        tamanho = os.path.getsize(final) / (1024 ** 2)

        secao('ENVIANDO PARA O DRIVE')
        enviado = enviar_para_drive(drive, final, pasta_destino_id, cfg['nome_arquivo'])

        # Guarda uma cópia para o GitHub disponibilizar como anexo
        destino_anexo = os.path.join(os.getcwd(), 'video_final')
        os.makedirs(destino_anexo, exist_ok=True)
        shutil.copy2(final, os.path.join(destino_anexo, cfg['nome_arquivo']))

        secao('✅ VÍDEO PRONTO')
        log(f'  Arquivo .... {cfg["nome_arquivo"]}')
        log(f'  Tamanho .... {tamanho:.1f} MB')
        log(f'  Duração .... {fmt_duracao(len(escolhidas) / FPS)}')
        log(f'  Link ....... {enviado.get("webViewLink", "(no Drive)")}')
        log(f'  Tempo total  {fmt_duracao(time.time() - inicio_geral)}')

        resumo = os.environ.get('GITHUB_STEP_SUMMARY')
        if resumo:
            with open(resumo, 'a') as f:
                f.write('## 🎬 Time-lapse gerado\n\n')
                f.write(f'| | |\n|---|---|\n')
                f.write(f'| Arquivo | `{cfg["nome_arquivo"]}` |\n')
                f.write(f'| Período | {cfg["data_inicio"]} a {cfg["data_fim"]} |\n')
                f.write(f'| Duração | {fmt_duracao(len(escolhidas) / FPS)} |\n')
                f.write(f'| Tamanho | {tamanho:.1f} MB |\n')
                f.write(f'| Resolução | {cfg["largura"]}x{cfg["altura"]} |\n\n')
                f.write(f'[Abrir no Google Drive]({enviado.get("webViewLink", "#")})\n')
    finally:
        shutil.rmtree(trabalho, ignore_errors=True)


if __name__ == '__main__':
    main()
