FROM python:3.13-slim


ENV PYTHONUNBUFFERED=1
WORKDIR /app


# Bluetooth + bleak deps
RUN apt-get update && apt-get install -y \
bluez \
dbus \
libglib2.0-0 \
&& rm -rf /var/lib/apt/lists/*


COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m appuser
USER appuser

EXPOSE 8765

CMD ["python", "main.py"]