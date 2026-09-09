#!/usr/bin/env python3
"""
Monitoramento do Diário Oficial Eletrônico - TCM-BA
Lopes Consultoria Gestão Pública

- Publicações do DIA
- Lembrete das publicações dos ÚLTIMOS 7 DIAS

Executar diariamente via Agendador de Tarefas do Windows.
"""

import os
import re
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, timedelta
import urllib.parse

# =============================================================
# CONFIGURAÇÕES
# =============================================================

# Cada cliente tem um rótulo (exibição) e um termo de busca (o que realmente aparece no diário).
# O diário usa o formato "Prefeitura Municipal de ITARANTIM" e "Câmara Municipal de ARACATU".
# Usar o nome do município garante que a busca e a extração do trecho funcionem corretamente.
CLIENTES = [
    {"label": "Prefeitura de Itarantim",  "busca": "Itarantim",  "entidade": "Prefeitura"},
    {"label": "Prefeitura de Contendas",  "busca": "Contendas",  "entidade": "Prefeitura"},
    {"label": "Câmara de Contendas",      "busca": "Contendas",  "entidade": "Câmara"},
    {"label": "Câmara de Aracatu",        "busca": "Aracatu",    "entidade": "Câmara"},
]

EMAIL_DESTINO   = "contabilidade@lopesconsultoria.cnt.br"
SMTP_SERVIDOR   = "smtp.gmail.com"
SMTP_PORTA      = 587
EMAIL_REMETENTE = os.environ["EMAIL_REMETENTE"]   # definido nas Secrets do GitHub
EMAIL_SENHA     = os.environ["EMAIL_SENHA"]        # definido nas Secrets do GitHub

JANELA_CONTEXTO = 400   # fallback: caracteres ao redor da palavra quando não há delimitador "Processo"

# =============================================================

BASE_URL = "https://egbanet.egba.ba.gov.br"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": BASE_URL + "/buscanova/",
}


def buscar(session: requests.Session, cliente: dict, di: str, df: str) -> list[dict]:
    """
    Consulta a API do TCM-BA para o cliente no intervalo de datas.
    Filtra por entidade (Prefeitura ou Câmara) no trecho encontrado.
    di, df: formato YYYY-MM-DD
    """
    termo    = cliente["busca"]
    entidade = cliente["entidade"]
    label    = cliente["label"]

    q = f'"{termo}"'
    url = (
        f"{BASE_URL}/busca/busca/buscar/query/0"
        f"/di:{di}/df:{df}/"
        f"?1=1&q={urllib.parse.quote(q)}&subtheme=tcm"
    )

    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        dados = r.json()
    except Exception as e:
        print(f"  [ERRO] {label} ({di}→{df}): {e}")
        return []

    hits = dados.get("hits", {}).get("hits", [])
    publicacoes = []

    for hit in hits:
        source   = hit.get("_source", {})
        conteudo = source.get("conteudo", "")

        # Extrai todos os trechos que mencionam o município
        trechos = _extrair_todos_trechos(conteudo, termo)

        for trecho in trechos:
            # Filtra: só inclui se o trecho menciona a entidade certa (Prefeitura ou Câmara)
            if entidade.lower() not in trecho.lower():
                continue

            data_pub = _extrair_data_conteudo(conteudo) or di
            publicacoes.append({
                "label":    label,
                "trecho":   trecho,
                "data_pub": data_pub,
                "doc_id":   hit.get("_id", ""),
            })

    return publicacoes


def _extrair_todos_trechos(texto: str, termo: str) -> list[str]:
    """
    Para cada ocorrência do termo, extrai o bloco completo do processo:
    do cabeçalho 'Processo …' mais próximo anterior até o próximo
    cabeçalho 'Processo …' (ambos identificados no início de linha).
    Deduplica por posição de início para evitar repetir o mesmo bloco
    quando o município aparece mais de uma vez dentro do mesmo processo.
    """
    # Padrão que identifica o início de uma nova entrada de processo.
    # Cobre: "Processo nº", "Processo TCM nº", "Processo n.°", "Processo e-TCM", etc.
    PAD = re.compile(r'(?:^|\n)[ \t]*processo\b', re.IGNORECASE)

    # Pré-calcula todas as posições de início de processo no texto
    posicoes = [m.start() for m in PAD.finditer(texto)]

    trechos = []
    vistos  = set()
    texto_lower = texto.lower()
    termo_lower = termo.lower()

    idx = 0
    while True:
        idx = texto_lower.find(termo_lower, idx)
        if idx == -1:
            break

        # Início: o cabeçalho "Processo" imediatamente anterior à ocorrência
        inicio = None
        for pos in reversed(posicoes):
            if pos <= idx:
                inicio = pos
                break
        if inicio is None:
            inicio = max(0, idx - JANELA_CONTEXTO)

        # Fim: o cabeçalho "Processo" seguinte (exclusive) — ou o final do texto
        fim = len(texto)
        for pos in posicoes:
            if pos > idx + len(termo):
                fim = pos
                break

        if inicio not in vistos:
            vistos.add(inicio)
            trecho = texto[inicio:fim].strip()
            if inicio > 0:
                trecho = "..." + trecho
            trechos.append(trecho)

        idx += len(termo)

    return trechos


def _extrair_data_conteudo(texto: str) -> str:
    """Tenta extrair a data da edição mencionada no cabeçalho do diário."""
    # Padrão: "SEGUNDA-FEIRA 1 DE SETEMBRO DE 2026" etc.
    meses = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
        "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
        "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
    }
    m = re.search(r"(\d{1,2})\s+DE\s+(\w+)\s+DE\s+(\d{4})", texto, re.IGNORECASE)
    if m:
        dia = m.group(1).zfill(2)
        mes = meses.get(m.group(2).lower(), "??")
        ano = m.group(3)
        return f"{dia}/{mes}/{ano}"
    return ""


def enviar_email(
    hoje_resultados: dict[str, list],
    semana_resultados: dict[str, list],
    data_br: str,
    di_semana_br: str,
    df_semana_br: str,
):
    """Envia e-mail com publicações do dia e lembrete da semana."""
    total_hoje   = sum(len(v) for v in hoje_resultados.values())
    total_semana = sum(len(v) for v in semana_resultados.values())

    if total_hoje == 0 and total_semana == 0:
        return  # Nada a reportar

    linhas = []

    # ── Cabeçalho ──────────────────────────────────────────────
    linhas.append("""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto">
    <div style="background:#003399;padding:16px 20px;border-radius:6px 6px 0 0">
      <h2 style="color:#fff;margin:0;font-size:18px">📋 Diário TCM-BA — Monitoramento</h2>
      <p style="color:#cce0ff;margin:4px 0 0;font-size:13px">Lopes Consultoria Gestão Pública</p>
    </div>
    """)

    # ── Seção: Publicações de HOJE ──────────────────────────────
    cor_hoje = "#006600" if total_hoje > 0 else "#888"
    icone_hoje = "🟢" if total_hoje > 0 else "⬜"
    linhas.append(f"""
    <div style="border:1px solid #ddd;border-top:none;padding:20px">
      <h3 style="color:{cor_hoje};margin:0 0 12px;border-bottom:2px solid {cor_hoje};padding-bottom:6px">
        {icone_hoje} Publicações de HOJE — {data_br}
      </h3>
    """)

    if total_hoje == 0:
        linhas.append("<p style='color:#888;font-style:italic'>Nenhuma publicação encontrada hoje para os clientes monitorados.</p>")
    else:
        for label, pubs in hoje_resultados.items():
            if not pubs:
                continue
            linhas.append(f"<h4 style='color:#003399;margin:14px 0 6px'>🔹 {label} <span style='font-weight:normal;font-size:13px'>({len(pubs)} ocorrência(s))</span></h4>")
            for p in pubs:
                trecho = p["trecho"].replace("<", "&lt;").replace(">", "&gt;")
                linhas.append(f"""
                <div style="background:#f0f7f0;border-left:4px solid #006600;padding:10px 14px;margin:6px 0;border-radius:0 4px 4px 0">
                  <span style="font-size:12px;font-family:monospace;white-space:pre-wrap;color:#222">{trecho}</span>
                </div>""")

    linhas.append("</div>")

    # ── Seção: Lembrete dos últimos 7 dias ─────────────────────
    linhas.append(f"""
    <div style="border:1px solid #ddd;border-top:none;padding:20px;background:#fafafa">
      <h3 style="color:#885500;margin:0 0 12px;border-bottom:2px solid #cc8800;padding-bottom:6px">
        🔔 Lembrete — Últimos 7 dias ({di_semana_br} a {df_semana_br})
      </h3>
    """)

    if total_semana == 0:
        linhas.append("<p style='color:#888;font-style:italic'>Nenhuma publicação nos últimos 7 dias.</p>")
    else:
        for label, pubs in semana_resultados.items():
            if not pubs:
                continue
            linhas.append(f"<h4 style='color:#885500;margin:14px 0 6px'>🔸 {label} <span style='font-weight:normal;font-size:13px'>({len(pubs)} ocorrência(s))</span></h4>")
            for p in pubs:
                data_pub = p.get("data_pub", "")
                data_label = f"<span style='font-size:11px;color:#888;float:right'>{data_pub}</span>" if data_pub else ""
                trecho = p["trecho"].replace("<", "&lt;").replace(">", "&gt;")
                linhas.append(f"""
                <div style="background:#fff8ee;border-left:4px solid #cc8800;padding:10px 14px;margin:6px 0;border-radius:0 4px 4px 0">
                  {data_label}
                  <div style="clear:both"></div>
                  <span style="font-size:12px;font-family:monospace;white-space:pre-wrap;color:#333">{trecho}</span>
                </div>""")

    linhas.append("</div>")

    # ── Rodapé ─────────────────────────────────────────────────
    linhas.append(f"""
    <div style="border:1px solid #ddd;border-top:none;padding:12px 20px;background:#f5f5f5;border-radius:0 0 6px 6px;text-align:center">
      <a href="{BASE_URL}/tcm" style="color:#003399;font-size:12px">Acessar Diário TCM-BA</a>
      <span style="color:#ccc;margin:0 8px">|</span>
      <span style="color:#aaa;font-size:11px">Monitoramento automático — Lopes Consultoria</span>
    </div>
    </div>
    """)

    # ── Montagem e envio ────────────────────────────────────────
    assunto_hoje = f"✅ {total_hoje} publicação(ões) HOJE" if total_hoje > 0 else "📭 Sem publicações hoje"
    assunto = f"[TCM-BA] {assunto_hoje} — {data_br}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"]    = EMAIL_REMETENTE
    msg["To"]      = EMAIL_DESTINO
    msg.attach(MIMEText("\n".join(linhas), "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_SERVIDOR, SMTP_PORTA, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_REMETENTE, EMAIL_SENHA)
            smtp.send_message(msg)
        print(f"✅ E-mail enviado para {EMAIL_DESTINO}")
    except Exception as e:
        print(f"❌ Erro ao enviar e-mail: {e}")
        print("   Verifique EMAIL_REMETENTE e EMAIL_SENHA no script.")


def main():
    hoje     = date.today()
    sete_dias_atras = hoje - timedelta(days=7)
    ontem    = hoje - timedelta(days=1)

    data_br       = hoje.strftime("%d/%m/%Y")
    di_semana_br  = sete_dias_atras.strftime("%d/%m/%Y")
    df_semana_br  = ontem.strftime("%d/%m/%Y")

    hoje_iso   = hoje.strftime("%Y-%m-%d")
    di_iso     = sete_dias_atras.strftime("%Y-%m-%d")
    df_iso     = ontem.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  Monitoramento TCM-BA — {data_br}")
    print(f"{'='*60}")

    session = requests.Session()
    try:
        session.get(BASE_URL + "/tcm", headers=HEADERS, timeout=15)
    except Exception:
        pass

    # ── Busca de HOJE ───────────────────────────────────────────
    print(f"\n📅 PUBLICAÇÕES DE HOJE ({data_br})")
    hoje_resultados: dict[str, list] = {}
    for cliente in CLIENTES:
        label = cliente["label"]
        print(f"  🔍 {label} ...", end=" ", flush=True)
        pubs = buscar(session, cliente, hoje_iso, hoje_iso)
        hoje_resultados[label] = pubs
        print(f"{'✅ ' + str(len(pubs)) + ' encontrada(s)' if pubs else '— nenhuma'}")

    # ── Busca dos ÚLTIMOS 7 DIAS ────────────────────────────────
    print(f"\n🔔 ÚLTIMOS 7 DIAS ({di_semana_br} → {df_semana_br})")
    semana_resultados: dict[str, list] = {}
    for cliente in CLIENTES:
        label = cliente["label"]
        print(f"  🔍 {label} ...", end=" ", flush=True)
        pubs = buscar(session, cliente, di_iso, df_iso)
        semana_resultados[label] = pubs
        print(f"{'✅ ' + str(len(pubs)) + ' encontrada(s)' if pubs else '— nenhuma'}")

    # ── Resumo ──────────────────────────────────────────────────
    total_hoje   = sum(len(v) for v in hoje_resultados.values())
    total_semana = sum(len(v) for v in semana_resultados.values())

    print(f"\n{'='*60}")
    print(f"  Hoje: {total_hoje} | Últimos 7 dias: {total_semana}")
    print(f"{'='*60}\n")

    if total_hoje > 0 or total_semana > 0:
        print("📧 Enviando e-mail...")
        enviar_email(hoje_resultados, semana_resultados, data_br, di_semana_br, df_semana_br)
    else:
        print("📭 Nenhuma publicação encontrada. Nenhum e-mail enviado.")


if __name__ == "__main__":
    main()
