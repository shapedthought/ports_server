import sqlite3 

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from io import BytesIO
import pandas as pd

app = Flask(__name__)
CORS(app)

def get_db_connection():
    conn = sqlite3.connect('allports.db')
    return conn

@app.route('/', methods=['GET'])
def get_services():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT Product FROM all_ports")
    services = cursor.fetchall()
    conn.close()
    flat_services = [item for sublist in services for item in sublist]
    return jsonify(flat_services)

@app.route('/source', methods=['POST'])
def get_product():
    data = request.get_json()
    print(data)
    product_name = data['productName']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT from_port FROM all_ports WHERE Product=?", (product_name,))
    product = cursor.fetchall()
    conn.close()
    flat_product = [item for sublist in product for item in sublist]
    return jsonify(flat_product)

@app.route('/target', methods=['POST'])
def get_target():
    data = request.get_json()
    from_port = data['fromPort']
    product_name = data['productName']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT to_port FROM all_ports WHERE from_port=? AND Product=?", (from_port, product_name,))
    to_ports = cursor.fetchall()
    conn.close()

    to_ports = [item for sublist in to_ports for item in sublist]
    return jsonify(to_ports)

@app.route('/allTarget', methods=['POST'])
def get_all_target():
    data = request.get_json()
    from_port = data['fromPort']
    product_name = data['productName']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM all_ports WHERE from_port=? AND Product=?", (from_port, product_name,))
    to_ports = cursor.fetchall()
    conn.close()

    column_names = [desc[0] for desc in cursor.description]
    to_ports = [dict(zip(column_names, port)) for port in to_ports]

    # Convert from_port and to_port keys to camelCase
    for port_dict in to_ports:
        port_dict['description'] = port_dict.pop('Description')
        port_dict['product'] = port_dict.pop('Product')
        port_dict['fromPort'] = port_dict.pop('from_port')
        port_dict['toPort'] = port_dict.pop('to_port')

    return jsonify(to_ports)

@app.route('/generateExcel', methods=['POST'])
def gnerate_excel():
    data = request.get_json()
    df = pd.DataFrame(data)
    
    # Create a BytesIO buffer
    buffer = BytesIO()
    
    # Write the DataFrame to an Excel file in the buffer
    df.to_excel(buffer, index=False)
    buffer.seek(0)
    
    # Send the file for download
    return send_file(
        buffer,
        as_attachment=True,
        download_name='example.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/ports', methods=['POST'])
def get_ports():
    data = request.get_json()
    from_port = data['fromPort']
    to_port = data['toPort']
    product_name = data['productName']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM all_ports WHERE from_port=? AND to_port=? AND Product=?", (from_port, to_port, product_name,))

    ports = cursor.fetchall()
    conn.close()

    column_names = [desc[0] for desc in cursor.description]
    ports_dict_list = [dict(zip(column_names, port)) for port in ports]

    # Convert from_port and to_port keys to camelCase
    for port_dict in ports_dict_list:
        port_dict['description'] = port_dict.pop('Description')
        port_dict['product'] = port_dict.pop('Product')
        port_dict['fromPort'] = port_dict.pop('from_port')
        port_dict['toPort'] = port_dict.pop('to_port')

    return jsonify(ports_dict_list)

# if __name__ == '__main__':
#     app.run(host='0.0.0.0')