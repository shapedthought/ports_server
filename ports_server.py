import os
import sqlite3
import uuid

from flask import Flask, request, jsonify, send_file, url_for
from flask_cors import CORS
import time
import pandas as pd

app = Flask(__name__)
CORS(app)


def get_db_connection():
    # Create a connection to the SQLite database
    conn = sqlite3.connect("allports.db")
    return conn


def clean_up_old_files(directory, min_age_minutes=1):
    # Remove files older than min_age_minutes from the directory
    # Stops the directory from getting filled with old files
    current_time = time.time()

    files = os.listdir(directory)
    if len(files) > 1:
        for filename in files:
            file_path = os.path.join(directory, filename)
            if os.path.isfile(file_path):
                file_creation_time = os.path.getctime(file_path)
                file_age_minutes = (current_time - file_creation_time) / 60
                if file_age_minutes > min_age_minutes:
                    os.remove(file_path)


@app.route("/health", methods=["GET"])
def health_check():
    # Simple health check endpoint
    return jsonify({"status": "healthy"}), 200


@app.route("/", methods=["GET"])
def get_services():
    # Get all the distinct services from the database
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT Product FROM all_ports")
    services = cursor.fetchall()
    conn.close()
    # Flatten the list of tuples
    flat_services = [item for sublist in services for item in sublist]
    return jsonify(flat_services)


@app.route("/source", methods=["POST"])
def get_product():
    # Get all the distinct source services for a given product
    data = request.get_json()
    print(data)
    product_name = data["productName"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT from_port FROM all_ports WHERE Product=?", (product_name,)
    )
    product = cursor.fetchall()
    conn.close()
    # Flatten the list of tuples
    flat_product = [item for sublist in product for item in sublist]
    return jsonify(flat_product)


@app.route("/target", methods=["POST"])
def get_target():
    # Get all the distinct target services for a given product and source service
    data = request.get_json()
    from_port = data["fromPort"]
    product_name = data["productName"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT to_port FROM all_ports " "WHERE from_port=? AND Product=?",
        (
            from_port,
            product_name,
        ),
    )
    to_ports = cursor.fetchall()
    conn.close()
    # Flatten the list of tuples
    to_ports = [item for sublist in to_ports for item in sublist]
    return jsonify(to_ports)


@app.route("/allTarget", methods=["POST"])
def get_all_target():
    # Get all the target services for a given product and source service
    data = request.get_json()
    from_port = data["fromPort"]
    product_name = data["productName"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM all_ports " "WHERE from_port=? AND Product=?",
        (
            from_port,
            product_name,
        ),
    )
    to_ports = cursor.fetchall()
    conn.close()

    # Convert the first item in the cursor.description tuple to a list of column names
    column_names = [desc[0] for desc in cursor.description]

    # Convert the list of tuples to a list of dictionaries
    to_ports = [dict(zip(column_names, port)) for port in to_ports]

    # Convert from_port and to_port keys to camelCase
    for port_dict in to_ports:
        port_dict["description"] = port_dict.pop("Description")
        port_dict["product"] = port_dict.pop("Product")
        port_dict["fromPort"] = port_dict.pop("from_port")
        port_dict["toPort"] = port_dict.pop("to_port")

    return jsonify(to_ports)


@app.route("/ports", methods=["POST"])
def get_ports():
    data = request.get_json()
    from_port = data["fromPort"]
    to_port = data["toPort"]
    product_name = data["productName"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM all_ports " "WHERE from_port=? AND to_port=? AND Product=?",
        (
            from_port,
            to_port,
            product_name,
        ),
    )
    ports = cursor.fetchall()
    print(ports)
    conn.close()

    # Convert the first item in the cursor.description tuple to a list of column names
    column_names = [desc[0] for desc in cursor.description]

    # Convert the list of tuples to a list of dictionaries
    ports_dict_list = [dict(zip(column_names, port)) for port in ports]

    # Convert from_port and to_port keys to camelCase
    for port_dict in ports_dict_list:
        port_dict["description"] = port_dict.pop("Description")
        port_dict["product"] = port_dict.pop("Product")
        port_dict["fromPort"] = port_dict.pop("from_port")
        port_dict["toPort"] = port_dict.pop("to_port")

    return jsonify(ports_dict_list)


@app.route("/generateExcelWithUrl", methods=["POST"])
def generate_excel_with_url():
    data = request.get_json()
    df = pd.DataFrame(data)

    clean_up_old_files("./reports")

    # Generate a unique filename
    unique_filename = f"{uuid.uuid4()}.xlsx"
    file_path = os.path.join("./reports", unique_filename)

    # Save the DataFrame to an Excel file
    df.to_excel(file_path, index=False)

    # Generate the URL for the file
    file_url = url_for("download_file", filename=unique_filename, _external=True)

    return jsonify({"file_url": file_url})


@app.route("/download/<filename>", methods=["GET"])
def download_file(filename):
    file_path = os.path.join("./reports", filename)

    # Send the file for download
    response = send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-officedocument." "spreadsheetml.sheet"
        ),
    )

    # Delete the file after sending
    os.remove(file_path)

    return response
