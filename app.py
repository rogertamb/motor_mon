from flask import Flask, render_template, jsonify, request
from db import get_all_data, get_last_execution_date

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


if __name__ == '__main__':
    print('Motor de Abastecimento – Monitor')
    print('Acesse: http://127.0.0.1:5000')
    app.run(debug=False, port=5000)
