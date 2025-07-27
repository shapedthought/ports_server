import os
import uuid
import time
import pandas as pd
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, MetaData, Table, select, distinct

# from pydantic import BaseModel
from typing import List
from models import PortResponse  # Your existing Pydantic model

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=False,  # Disables credential sharing (default)
    allow_methods=["*"],  # Allows all HTTP methods
    allow_headers=["*"],  # Allows all headers
)

# SQLAlchemy setup
engine = create_engine("sqlite:///allports_updated.db", future=True)
metadata = MetaData()
all_ports = Table("all_ports", metadata, autoload_with=engine)


def clean_up_old_files(directory, min_age_minutes=1):
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


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/")
async def get_services():
    stmt = select(distinct(all_ports.c.product))
    with engine.connect() as conn:
        result = conn.execute(stmt)
        services = [row[0] for row in result]
    return services


@app.post("/source")
async def get_product(request: Request):
    data = await request.json()
    product_name = data["productName"]
    stmt = select(distinct(all_ports.c.sourceService)).where(
        all_ports.c.product == product_name
    )
    with engine.connect() as conn:
        result = conn.execute(stmt)
        products = [row[0] for row in result]
    return products


@app.post("/target")
async def get_target(request: Request):
    data = await request.json()
    source_service = data["sourceService"]
    product_name = data["productName"]
    stmt = select(distinct(all_ports.c.targetService)).where(
        (all_ports.c.sourceService == source_service)
        & (all_ports.c.product == product_name)
    )
    with engine.connect() as conn:
        result = conn.execute(stmt)
        to_ports = [row[0] for row in result]
    return to_ports


@app.post("/allTarget", response_model=List[PortResponse])
async def get_all_target(request: Request):
    data = await request.json()
    source_service = data["sourceService"]
    product_name = data["productName"]
    stmt = (
        select(all_ports)
        .where(
            (all_ports.c.sourceService == source_service)
            & (all_ports.c.product == product_name)
        )
        .order_by(
            all_ports.c.subheading,
            all_ports.c.subheadingL2,
            all_ports.c.subheadingL3,
            all_ports.c.targetService,
        )
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    response_models = [PortResponse(**record) for record in records]
    return response_models


@app.post("/ports", response_model=List[PortResponse])
async def get_ports(request: Request):
    data = await request.json()
    source_service = data["sourceService"]
    target_service = data["targetService"]
    product_name = data["productName"]
    stmt = select(all_ports).where(
        (all_ports.c.sourceService == source_service)
        & (all_ports.c.targetService == target_service)
        & (all_ports.c.product == product_name)
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    response_models = [PortResponse(**record) for record in records]
    return response_models


@app.post("/generateExcelWithUrl")
async def generate_excel_with_url(data: List[dict]):
    df = pd.DataFrame(data)
    clean_up_old_files("./reports")
    unique_filename = f"{uuid.uuid4()}.xlsx"
    file_path = os.path.join("./reports", unique_filename)
    df.to_excel(file_path, index=False)
    file_url = f"/download/{unique_filename}"
    return {"file_url": file_url}


@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = os.path.join("./reports", filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    response = FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
    # Optionally, delete the file after sending (uncomment if desired)
    # os.remove(file_path)
    return response


# To run: uvicorn ports_server_fastapi:app --reload
