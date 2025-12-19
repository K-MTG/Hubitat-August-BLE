# Hubitat August BLE Service

Local-only August smart lock integration using a BLE-backed WebSocket service and Hubitat drivers. 
Provides reliable lock control, door state, and battery reporting without relying on the August cloud or constantly 
polling the lock.

```text
┌──────────────────────┐
│   August / Yale Lock │
│                      │
│   Bluetooth (BLE)    │
└───────────▲──────────┘
            │
            │ BLE
            │
┌───────────┴──────────┐
│  BLE Compute Device  │
│  (e.g. Raspberry Pi) │
│                      │
│  ble_ws_service      │
└───────────▲──────────┘
            │
            │ WebSocket (LAN)
            │
┌───────────┴──────────┐
│     Hubitat Hub      │
│                      │
└──────────────────────┘
```

---

## Prerequisites 

### Hardware
 - August / Yale Bluetooth Smart Lock
 - BLE-capable compute device
      - I'm using a Raspberry Pi CM5 in an enclosure with an external bluetooth antenna
 - Hubitat Hub

### Software
 - BLE Compute Device
   - OS: Linux
     - I used Ubuntu 24.04 server
   - Docker & Docker Compose
     - Used to run `ble_ws_service`
   - BlueZ (Linux Bluetooth stack)
 - Python 3
   - Used to run setup `august_cli` script

### Network
 - Hubitat initiates a persistent WebSocket connection to the BLE service on port `8765`
 - Static IP or FQDN for BLE compute host

### Credentials & Configuration
 - August / Yale Offline Key & Slot
   - Required for local BLE authentication
 - Lock metadata
   - Serial number
   - Bluetooth MAC address
 - WebSocket authentication token (Optional)
   - Shared secret between Hubitat and the BLE service

---

## Getting Started

_Note: For reliable status updates, the lock may not be paired directly with the Apple Home app. Home Assistant has 
[additional documentation](https://www.home-assistant.io/integrations/yalexs_ble/#push-updates) around this. 
The lock may still be shared with Apple Home via the Hubitat Hub._ 

### Obtain Lock Offline Key & Metadata
The lock offline key, slot number, serial, and bluetooth Mac are required.
The HomeAssistant documentation can be 
referenced for additional details: [Home Assistant Yale Access Bluetooth](https://www.home-assistant.io/integrations/yalexs_ble/#obtaining-the-offline-key)


The `august_cli.py` in `examples` can be used to obtain the locks offline key/slot and metadata. 
1. Ensure `python3` is installed with `pip`
2. Install requirements: `pip install -r examples/requirements.txt`
3. Execute cli: `python examples/august_cli.py`
4. Follow the interactive shell and save the response in a safe place

### Setup BLE Compute Device

The host **must** provide the Bluetooth stack.

1. [Install Docker & Docker Compose](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository)
2. Install & Enable Bluetooth dependencies
```bash
sudo apt install bluez dbus
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```
3. Clone repo
```bash
cd /opt
git clone https://github.com/K-MTG/Hubitat-August-BLE.git
cd Hubitat-August-BLE/ble_ws_service
```
4. Create `config.yaml` using `config_example.yaml` as reference. 
5. Start container `sudo docker compose up -d --build`
---

### Setup Hubitat Driver
1. In Hubitat, go to **Drivers Code**
2. Add the following drivers - import following URL
   - `https://raw.githubusercontent.com/K-MTG/Hubitat-August-BLE/refs/heads/main/drivers/august-ble-bridge-parent.groovy`
   - `https://raw.githubusercontent.com/K-MTG/Hubitat-August-BLE/refs/heads/main/drivers/august-ble-lock-child.groovy`
3. Create a virtual device using **August BLE Bridge (Parent)**
4. Configure the WebSocket host, port, and token under Preferences
5. Click **Initialize**

Child lock devices will be created automatically.

---

## Components

### BLE WS Service

A Python service that bridges **Yale/August BLE locks** to a **WebSocket API**, suitable for Hubitat or custom 
automation systems.

The service:

* 📡 Connects to Yale/August locks over **Bluetooth Low Energy (BLE)** using `yalexs_ble`
* 🚪 Emits **lock / door events** without polling. 
* 🌐 Exposes a **WebSocket API** with optional authentication
* 🔐 Supports multiple locks
* 🐳 Docker + docker-compose ready


#### Architecture Overview

```text
Yale/August Lock (BLE)
        ↓
   yalexs_ble
        ↓ (sync callback)
     BleLock
        ↓ (async fan-out)
   LockManager
        ↓ (async broadcast)
 WebSocket Server
        ↓
     Clients
```

Each layer has a single responsibility:

* **BleLock**: adapts synchronous BLE callbacks to async Python
* **LockManager**: normalizes state and emits meaningful events
* **WebSocketServer**: handles clients and authentication

---