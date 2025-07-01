# -*- coding: utf-8 -*-

import os
import sqlite3
from datetime import datetime
from flask import Flask, request, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd

# Carrega as variáveis de ambiente do arquivo .env (para desenvolvimento local)
# Em produção (PythonAnywhere), configuraremos isso de outra forma.
load_dotenv()

# --- Configuração Inicial ---
app = Flask(__name__)

# Credenciais da Twilio e número do administrador a partir das variáveis de ambiente
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
ADMIN_PHONE_NUMBER = os.getenv('ADMIN_PHONE_NUMBER') # Formato: "whatsapp:+5571..."

# Cliente da Twilio
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Nome do arquivo do banco de dados
DB_NAME = 'participations.db'

# --- Funções do Banco de Dados (SQLite) ---

def init_db():
    """Cria a tabela no banco de dados se ela não existir."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS participations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            whatsapp_number TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            participation_date TIMESTAMP NOT NULL,
            video_title TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_participation(whatsapp_number, profile_name, video_title=None):
    """Adiciona um novo registro de participação."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO participations (whatsapp_number, profile_name, participation_date, video_title) VALUES (?, ?, ?, ?)",
        (whatsapp_number, profile_name, datetime.now(), video_title)
    )
    conn.commit()
    conn.close()

def get_user_history(whatsapp_number):
    """Busca o histórico de participações de um usuário."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT participation_date, video_title FROM participations WHERE whatsapp_number = ? ORDER BY participation_date DESC",
        (whatsapp_number,)
    )
    records = cursor.fetchall()
    conn.close()
    return records

def get_last_10_records():
    """Busca os últimos 10 registros de participação."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT profile_name, participation_date, video_title FROM participations ORDER BY participation_date DESC LIMIT 10")
    records = cursor.fetchall()
    conn.close()
    return records

def delete_last_user_record(whatsapp_number):
    """Deleta o último registro de um usuário específico."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Encontra o ID do último registro do usuário
    cursor.execute(
        "SELECT id FROM participations WHERE whatsapp_number = ? ORDER BY participation_date DESC LIMIT 1",
        (whatsapp_number,)
    )
    last_record = cursor.fetchone()
    if last_record:
        cursor.execute("DELETE FROM participations WHERE id = ?", (last_record[0],))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# --- Lógica do Relatório ---

def generate_and_send_report(on_demand=False):
    """Gera o relatório, salva em CSV/XLSX e envia para o admin."""
    with app.app_context():
        conn = sqlite3.connect(DB_NAME)
        # Usamos o Pandas para ler diretamente do SQL e facilitar a manipulação
        try:
            df = pd.read_sql_query("SELECT * FROM participations", conn)
        except pd.io.sql.DatabaseError:
            df = pd.DataFrame() # Cria um DataFrame vazio se a tabela não existir ou estiver vazia
        finally:
            conn.close()

        if df.empty:
            if on_demand:
                client.messages.create(
                    from_=TWILIO_PHONE_NUMBER,
                    body="ℹ️ Nenhum registro de participação encontrado para gerar o relatório.",
                    to=ADMIN_PHONE_NUMBER
                )
            return

        # Converte a data para um formato legível
        df['participation_date'] = pd.to_datetime(df['participation_date']).dt.strftime('%d/%m/%Y %H:%M')

        # Agrupa os dados para o relatório
        report_summary = df.groupby('profile_name').agg(
            total_participations=('id', 'count'),
            dates=('participation_date', lambda x: ' | '.join(x)),
            videos=('video_title', lambda x: ' | '.join(x.dropna()))
        ).reset_index()

        # Cria o diretório 'reports' se não existir
        if not os.path.exists('reports'):
            os.makedirs('reports')

        # Salva os arquivos de relatório
        report_path_csv = os.path.join('reports', 'relatorio_participacao.csv')
        report_path_xlsx = os.path.join('reports', 'relatorio_participacao.xlsx')
        report_summary.to_csv(report_path_csv, index=False, encoding='utf-8-sig')
        report_summary.to_excel(report_path_xlsx, index=False)

        # Envia a mensagem de resumo e o link para o admin
        summary_message = "📊 *Relatório de Participação*\n\n"
        for _, row in report_summary.iterrows():
            summary_message += f"*{row['profile_name']}*: {row['total_participations']} participações\n"

        # IMPORTANTE: A Twilio não envia arquivos diretamente.
        # A solução é hospedar o arquivo e enviar o link.
        # O guia de implantação abaixo mostrará como fazer isso no PythonAnywhere.
        base_url = request.host_url if request else "https://SEU_USUARIO.pythonanywhere.com/"
        link_xlsx = f"{base_url}reports/relatorio_participacao.xlsx"

        summary_message += f"\nBaixe o relatório completo em Excel aqui:\n{link_xlsx}"

        client.messages.create(
            from_=TWILIO_PHONE_NUMBER,
            body=summary_message,
            to=ADMIN_PHONE_NUMBER
        )

# --- Agendador de Tarefas (APScheduler) ---

scheduler = BackgroundScheduler(daemon=True)
# Roda nos dias 1 e 15 de cada mês, às 10:00 da manhã.
scheduler.add_job(generate_and_send_report, 'cron', day='1,15', hour=10)
scheduler.start()


# --- Rota Principal do Webhook ---

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Recebe e processa as mensagens do WhatsApp via Twilio."""
    incoming_msg = request.values.get('Body', '').strip()
    from_number = request.values.get('From', '')
    profile_name = request.values.get('ProfileName', from_number) # Usa o nome do perfil, ou o número como fallback
    msg_lower = incoming_msg.lower()
    
    response_msg = ""

    # --- Comandos para Membros ---
    if msg_lower.startswith(('participei', 'gravei', '/participar')):
        video_title = None
        if ':' in incoming_msg:
            video_title = incoming_msg.split(':', 1)[1].strip()
        add_participation(from_number, profile_name, video_title)
        response_msg = f"✅ Olá, {profile_name}! Sua participação foi registrada com sucesso!"

    elif msg_lower == '/meu historico':
        records = get_user_history(from_number)
        if not records:
            response_msg = "Você ainda não tem nenhuma participação registrada."
        else:
            response_msg = f"Olá, {profile_name}! Você participou *{len(records)}* vez(es).\n\n*Datas*:\n"
            for record in records:
                date_formatted = datetime.strptime(record['participation_date'], '%Y-%m-%d %H:%M:%S.%f').strftime('%d/%m/%Y')
                title_info = f" (Vídeo: {record['video_title']})" if record['video_title'] else ""
                response_msg += f"- {date_formatted}{title_info}\n"

    elif msg_lower == '/ajuda':
        response_msg = (
            "🤖 *Comandos do Bot de Gravações*\n\n"
            "*/participar* - Registra sua participação na gravação de hoje.\n_Ex: `Participei: vídeo de highlights`_\n\n"
            "*/meu historico* - Mostra todas as suas participações.\n\n"
            "*/ajuda* - Mostra esta mensagem."
        )

    # --- Comandos para Administrador ---
    elif from_number == ADMIN_PHONE_NUMBER:
        if msg_lower == '/relatorio agora':
            generate_and_send_report(on_demand=True)
            # A confirmação é o próprio relatório enviado.
            return '', 204

        elif msg_lower == '/ultimos registros':
            records = get_last_10_records()
            if not records:
                response_msg = "Nenhum registro encontrado."
            else:
                response_msg = "*Últimos 10 Registros*:\n\n"
                for r in records:
                    date_formatted = datetime.strptime(r['participation_date'], '%Y-%m-%d %H:%M:%S.%f').strftime('%d/%m %H:%M')
                    title_info = f" ({r['video_title']})" if r['video_title'] else ""
                    response_msg += f"- {r['profile_name']} em {date_formatted}{title_info}\n"

        elif msg_lower.startswith('/corrigir ultimo'):
            # Pega o número do usuário a ser corrigido, se especificado
            parts = msg_lower.split()
            if len(parts) > 2:
                # Lógica para corrigir o último de um usuário específico (avançado)
                pass
            else: # Corrige o último registro de quem enviou o comando (o admin)
                if delete_last_user_record(from_number):
                    response_msg = "✅ Seu último registro de participação foi removido. Por favor, registre novamente se necessário."
                else:
                    response_msg = "❌ Você não possui registros para corrigir."
    
    # Envia a resposta se houver alguma
    if response_msg:
        resp = MessagingResponse()
        resp.message(response_msg)
        return str(resp)

    return '', 204 # Retorna uma resposta vazia para não dar erro na Twilio

# Rota para servir os arquivos de relatório
@app.route('/reports/<filename>')
def serve_report(filename):
    return send_from_directory('reports', filename)


# Inicializa o banco de dados ao iniciar a aplicação
with app.app_context():
    init_db()

# O if __name__ == '__main__' é para execução local.
# O PythonAnywhere usa um método diferente para iniciar o app.
if __name__ == '__main__':
    app.run(port=5000, debug=True)

