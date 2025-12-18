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
 */

metadata {
    definition(
        name: "August BLE Lock (Child)",
        namespace: "k-mtg",
        author: "K-MTG",
        importUrl: "https://raw.githubusercontent.com/K-MTG/Hubitat-August-BLE/refs/heads/main/drivers/august-ble-lock-child.groovy"
    ) {
        capability "Lock"
        capability "Refresh"
        capability "ContactSensor"
        capability "Battery"

        attribute "rssi", "number"
        attribute "manufacturer", "string"
        attribute "model", "string"
        attribute "serialNumber", "string"
    }

    preferences {
        input name: "debugLogging", type: "bool", title: "Enable debug logging", defaultValue: false
    }
}

def installed() {
    logInfo "Installed"
}

def updated() {
    logInfo "Updated"
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

/**
 * Called by the parent when an event/response arrives for this lock.
 */
def applySnapshot(Map s) {
    if (s == null) return

    // LOCK
    if (s.locked != null) {
        def lockedRaw = s.locked.toString().toUpperCase()
        switch (lockedRaw) {
            case "LOCKED":
                sendEvent(name: "lock", value: "locked")
                break
            case "UNLOCKED":
                sendEvent(name: "lock", value: "unlocked")
                break
            default:
                logWarn "Unknown lock state '${s.locked}'"
        }
    }

    // CONTACT (door sensor)
    if (s.door != null) {
        def doorRaw = s.door.toString().toUpperCase()
        switch (doorRaw) {
            case "OPENED":
                sendEvent(name: "contact", value: "open")
                break
            case "CLOSED":
                sendEvent(name: "contact", value: "closed")
                break
            default:
                logWarn "Unknown door state '${s.door}'"
        }
    }

    // BATTERY
    if (s.battery_pct != null) {
        try {
            sendEvent(name: "battery", value: (s.battery_pct as Integer))
        } catch (e) {
            logWarn "Invalid battery_pct '${s.battery_pct}'"
        }
    }

    // RSSI
    if (s.rssi != null) {
        try {
            sendEvent(name: "rssi", value: (s.rssi as Integer))
        } catch (e) {
            logWarn "Invalid rssi '${s.rssi}'"
        }
    }

    // METADATA
    if (s.manufacturer) sendEvent(name: "manufacturer", value: s.manufacturer.toString())
    if (s.model)        sendEvent(name: "model", value: s.model.toString())
    if (s.serial)       sendEvent(name: "serialNumber", value: s.serial.toString())
}

private String getLockName() {
    device.getDataValue("lock_name") ?: device.label
}

/* ================= Logging ================= */

private logDebug(msg) { if (debugLogging) log.debug "${device.displayName}: ${msg}" }
private logInfo(msg)  { log.info  "${device.displayName}: ${msg}" }
private logWarn(msg)  { log.warn  "${device.displayName}: ${msg}" }
