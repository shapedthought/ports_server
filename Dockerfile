FROM python:3.12.10-slim

WORKDIR /app

COPY . /app

RUN mkdir /app/reports

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "ports_server:app"]