from flask import Flask, render_template, jsonify, request
from db import get_all_data, get_last_execution_date, get_failure_history

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/last-execution-date')
def api_last_date():
    return jsonify({'date': get_last_execution_date()})


@app.route('/api/data')
def api_data():
    target_date = request.args.get('date')  # 'YYYY-MM-DD' ou None
    return jsonify(get_all_data(target_date))


@app.route('/api/failure-history')
def api_failure_history():
    date_from = request.args.get('from')   # 'YYYY-MM-DD'
    date_to   = request.args.get('to')     # 'YYYY-MM-DD'
    days      = int(request.args.get('days', 90))
    return jsonify(get_failure_history(date_from=date_from,
                                       date_to=date_to,
                                       days=days))


if __name__ == '__main__':
    print('Motor de Abastecimento – Monitor')
    print('Acesse: http://127.0.0.1:5000')
    app.run(debug=False, port=5000)
