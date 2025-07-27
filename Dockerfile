FROM python:3.12.10-slim

WORKDIR /app

COPY . /app

RUN mkdir /app/reports

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8001

CMD ["uvicorn", "ports_server:app", "--host", "0.0.0.0", "--port", "8001"]