import os
import sqlite3
import uuid
import traceback
from models import (
    PortsModel,
    ProductRequest,
    SourceTargetRequest,
    PortsRequest,
    ExcelDataRequest,
    ErrorResponse,
    FileUrlResponse,
    SourceModel,
)
from flask import Flask, request, jsonify, send_file, url_for
from flask_cors import CORS
import time
import pandas as pd
from pydantic import ValidationError

app = Flask(__name__)
CORS(app)


# Helper function for request validation
def validate_request(model_class):
    """
    Validate request data with the given Pydantic model.
    Returns (validated_data, error_response, status_code) tuple.
    """
    try:
        # Try to get and validate the JSON data
        json_data = request.get_json()
        if not json_data:
            return None, ErrorResponse(error="No JSON data provided").model_dump(), 400

        # Validate with Pydantic model
        if model_class == ExcelDataRequest:
            # Special case for Excel data which is a list
            validated_data = model_class(root=json_data)
            return json_data, None, None  # Return original data for Excel
        else:
            validated_data = model_class(**json_data)
            return validated_data, None, None
    except ValidationError as e:
        # Format validation errors
        errors = e.errors()
        error_messages = []
        for error in errors:
            loc = ".".join(str(x) for x in error["loc"])
            msg = f"{loc}: {error['msg']}"
            error_messages.append(msg)

        return (
            None,
            ErrorResponse(
                error="Validation failed", details=error_messages
            ).model_dump(),
            422,
        )
    except Exception as e:
        app.logger.error(f"Error during validation: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            None,
            ErrorResponse(error=f"An error occurred: {str(e)}").model_dump(),
            500,
        )


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


@app.route("/", methods=["GET"])
def get_services():
    """Get all the distinct services/products from the database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT product FROM all_ports")
        services = cursor.fetchall()
        conn.close()

        # Convert to list of dictionaries with UUIDs
        result = []
        for row in services:
            service_name = row[0]
            # Create a dictionary directly instead of using the model
            service_dict = {"id": str(uuid.uuid4()), "name": service_name}
            result.append(service_dict)

        print("/ endpoint run")
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Error in get_services: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            jsonify(ErrorResponse(error=f"Database error: {str(e)}").model_dump()),
            500,
        )


@app.route("/source", methods=["POST"])
def get_product():
    """Get all the distinct source services for a given product"""
    # Validate the request using ProductRequest model
    validated_data, error_response, status_code = validate_request(ProductRequest)
    if error_response:
        return jsonify(error_response), status_code

    # Use validated data
    product_name = validated_data.product_name

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT from_port, section FROM all_ports WHERE product=? ORDER BY section ASC, from_port ASC",
            (product_name,),
        )
        sources = cursor.fetchall()
        conn.close()

        # Convert to list of SourceModel objects with UUIDs
        result = []
        for row in sources:
            from_port = row[0]
            section = row[1]
            source = SourceModel(
                id=str(uuid.uuid4()),
                product=product_name,
                from_port=from_port,
                section=section,
            )
            result.append(source.model_dump(by_alias=True))

        print("source endpoint run")
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Error in get_product: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            jsonify(ErrorResponse(error=f"Database error: {str(e)}").model_dump()),
            500,
        )


@app.route("/target", methods=["POST"])
def get_target():
    """Get all the distinct target services for a given product and source service"""
    # Validate the request using SourceTargetRequest model
    validated_data, error_response, status_code = validate_request(SourceTargetRequest)
    if error_response:
        return jsonify(error_response), status_code

    # Use validated data
    from_port = validated_data.from_port
    product_name = validated_data.product_name

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT to_port FROM all_ports WHERE from_port=? AND product=?",
            (
                from_port,
                product_name,
            ),
        )
        to_ports = cursor.fetchall()
        conn.close()  # Convert to list of dictionaries with UUIDs
        result = []
        for row in to_ports:
            to_port = row[0]
            target_dict = {
                "id": str(uuid.uuid4()),
                "product": product_name,
                "toPort": to_port,
            }
            result.append(target_dict)

        print("target endpoint run")
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Error in get_target: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            jsonify(ErrorResponse(error=f"Database error: {str(e)}").model_dump()),
            500,
        )


@app.route("/allPorts", methods=["POST"])
def get_all_target():
    """Get all the target services for a given product and source service"""
    # Validate the request using SourceTargetRequest model
    validated_data, error_response, status_code = validate_request(SourceTargetRequest)
    if error_response:
        return jsonify(error_response), status_code

    # Use validated data
    from_port = validated_data.from_port
    product_name = validated_data.product_name
    section = validated_data.section

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT * FROM all_ports WHERE from_port=? AND product=? AND section=?",
            (
                from_port,
                product_name,
                section,
            ),
        )
        to_ports = cursor.fetchall()
        conn.close()

        # Convert the first item in the cursor.description tuple to a list of column names
        column_names = [desc[0] for desc in cursor.description]

        # Convert the list of tuples to a list of dictionaries
        to_ports = [
            dict(zip(column_names, port)) for port in to_ports
        ]  # Use Pydantic model to convert from_port and to_port keys to camelCase
        result = []
        for port_dict in to_ports:
            # Generate a unique ID for each record
            port_dict["id"] = str(uuid.uuid4())
            model = PortsModel(**port_dict)
            result.append(model.model_dump(by_alias=True))

        print("allTarget endpoint run")
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Error in get_all_target: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            jsonify(ErrorResponse(error=f"Database error: {str(e)}").model_dump()),
            500,
        )


@app.route("/ports", methods=["POST"])
def get_ports():
    """Get port details for a specific source, target and product combination"""
    # Validate the request using PortsRequest model
    validated_data, error_response, status_code = validate_request(PortsRequest)
    if error_response:
        return jsonify(error_response), status_code

    # Use validated data
    from_port = validated_data.from_port
    to_port = validated_data.to_port
    product_name = validated_data.product_name

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM all_ports WHERE from_port=? AND to_port=? AND product=?",
            (
                from_port,
                to_port,
                product_name,
            ),
        )
        ports = cursor.fetchall()
        print("ports endpoint run")
        conn.close()

        # Convert the first item in the cursor.description tuple to a list of column names
        column_names = [desc[0] for desc in cursor.description]

        # Convert the list of tuples to a list of dictionaries
        ports_dict_list = [
            dict(zip(column_names, port)) for port in ports
        ]  # Use Pydantic model to convert from_port and to_port keys to camelCase
        result = []
        for port_dict in ports_dict_list:
            # Generate a unique ID for each record
            port_dict["id"] = str(uuid.uuid4())
            model = PortsModel(**port_dict)
            result.append(model.model_dump(by_alias=True))

        print(result)
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Error in get_ports: {str(e)}")
        app.logger.error(traceback.format_exc())
        return jsonify(ErrorResponse(error=f"Database error: {str(e)}").dict()), 500


@app.route("/generateExcelWithUrl", methods=["POST"])
def generate_excel_with_url():
    """Generate Excel file from provided data and return download URL"""
    # Validate the request using ExcelDataRequest model
    validated_data, error_response, status_code = validate_request(ExcelDataRequest)
    if error_response:
        return jsonify(error_response), status_code

    try:
        # Create DataFrame from the validated data
        df = pd.DataFrame(validated_data)

        # Ensure the reports directory exists
        os.makedirs("./reports", exist_ok=True)
        clean_up_old_files("./reports")

        # Generate a unique filename
        unique_filename = f"{uuid.uuid4()}.xlsx"
        file_path = os.path.join("./reports", unique_filename)

        # Save the DataFrame to an Excel file
        df.to_excel(file_path, index=False)

        # Generate the URL for the file
        file_url = url_for("download_file", filename=unique_filename, _external=True)
        print("generateExcelWithUrl endpoint run")

        # Use Pydantic model for response
        response = FileUrlResponse(file_url=file_url)
        return jsonify(response.model_dump(by_alias=True))
    except Exception as e:
        app.logger.error(f"Error in generate_excel_with_url: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            jsonify(
                ErrorResponse(error=f"Excel generation error: {str(e)}").model_dump()
            ),
            500,
        )


@app.route("/download/<filename>", methods=["GET"])
def download_file(filename):
    """Download the generated Excel file"""
    try:
        file_path = os.path.join("./reports", filename)

        if not os.path.exists(file_path):
            return (
                jsonify(ErrorResponse(error=f"File {filename} not found").model_dump()),
                404,
            )

        # Send the file for download
        response = send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )

        # Delete the file after sending
        os.remove(file_path)
        print("download endpoint run")

        return response
    except Exception as e:
        app.logger.error(f"Error in download_file: {str(e)}")
        app.logger.error(traceback.format_exc())
        return (
            jsonify(ErrorResponse(error=f"Download error: {str(e)}").model_dump()),
            500,
        )


# Global error handlers
@app.errorhandler(400)
def bad_request(error):
    """Handle 400 Bad Request errors"""
    if request.method == "POST" and not request.is_json:
        return (
            jsonify(
                ErrorResponse(
                    error="Invalid JSON or incorrect Content-Type"
                ).model_dump()
            ),
            400,
        )
    return jsonify(ErrorResponse(error="Bad request").model_dump()), 400


@app.errorhandler(404)
def not_found(error):
    """Handle 404 Not Found errors"""
    return (
        jsonify(
            ErrorResponse(error=f"Resource not found: {request.path}").model_dump()
        ),
        404,
    )


@app.errorhandler(405)
def method_not_allowed(error):
    """Handle 405 Method Not Allowed errors"""
    return (
        jsonify(
            ErrorResponse(
                error=f"Method {request.method} not allowed for {request.path}"
            ).model_dump()
        ),
        405,
    )


@app.errorhandler(500)
def internal_server_error(error):
    """Handle 500 Internal Server Error"""
    app.logger.error(f"Internal server error: {str(error)}")
    return jsonify(ErrorResponse(error="Internal server error").model_dump()), 500
