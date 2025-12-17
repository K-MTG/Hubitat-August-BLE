/**
 *  August BLE Lock (Child)
 *
 *  Filename: august-ble-lock-child.groovy
 *  Version:  0.1.0
 *
 *  Description:
 *  - Represents a single August BLE lock
 *  - Controlled via parent WebSocket bridge
 *  - Supports Lock, ContactSensor, Battery
 *
 *  Changelog:
 *  0.1.0 (2025-12-14)
 *   - Initial public release
 *   - LOCKED / UNLOCKED mapping
 *   - Door state mapping (OPENED / CLOSED)
 *   - Battery and RSSI reporting
 */

metadata {
    definition(name: "August BLE Lock (Child)", namespace: "k-mtg", author: "K-MTG") {
        capability "Lock"
        capability "Refresh"
        capability "ContactSensor"
        capability "Battery"

        attribute "rssi", "number"
        attribute "manufacturer", "string"
        attribute "model", "string"
        attribute "serialNumber", "string"
    }
}

def refresh() {
    parent.childGetState(getLockName())
}

def lock() {
    parent.childLock(getLockName())
}

def unlock() {
    parent.childUnlock(getLockName())
}

def applySnapshot(Map s) {
    // LOCK
    if (s.locked) {
        def val = (s.locked == "LOCKED") ? "locked" : "unlocked"
        sendEvent(name: "lock", value: val)
    }

    // CONTACT (door sensor)
    if (s.door != null) {
        switch (s.door.toString().toUpperCase()) {
            case "OPENED":
                sendEvent(name: "contact", value: "open")
                break
            case "CLOSED":
                sendEvent(name: "contact", value: "closed")
                break
            default:
                log.warn "Unknown door state '${s.door}'"
        }
    }


    // BATTERY
    if (s.battery_pct != null) {
        sendEvent(name: "battery", value: s.battery_pct as Integer)
    }

    // RSSI
    if (s.rssi != null) {
        sendEvent(name: "rssi", value: s.rssi as Integer)
    }

    // METADATA
    if (s.manufacturer) sendEvent(name: "manufacturer", value: s.manufacturer)
    if (s.model) sendEvent(name: "model", value: s.model)
    if (s.serial) sendEvent(name: "serialNumber", value: s.serial)
}

private getLockName() {
    device.getDataValue("lock_name") ?: device.label
}
