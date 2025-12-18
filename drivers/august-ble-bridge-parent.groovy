/**
 * August BLE Bridge (Parent)
 *
 * Filename: august-ble-bridge-parent.groovy
 * Version:  0.1.0
 *
 * Description:
 * - Maintains a persistent WebSocket connection to an August BLE service
 * - Discovers locks via list_locks
 * - Creates/manages child lock devices
 * - Routes WS events to children
 * - Routes child commands to WS
 *
 * Changes (0.1.0):
 * - Initial Release
 */

import groovy.json.JsonOutput
import groovy.json.JsonSlurper

metadata {
    definition(
        name: "August BLE Bridge (Parent)",
        namespace: "k-mtg",
        author: "K-MTG",
        importUrl: "https://raw.githubusercontent.com/K-MTG/Hubitat-August-BLE/refs/heads/main/drivers/august-ble-bridge-parent.groovy"
    ) {
        capability "Initialize"
        capability "Refresh"

        command "Connect"
        command "Disconnect"

        attribute "is_connected", "bool"
        attribute "connection_status", "string"
    }

    preferences {
        input name: "wsHost", type: "string", title: "WebSocket Host / IP", required: true
        input name: "wsPort", type: "number", title: "WebSocket Port", required: true, defaultValue: 8765
        input name: "apiToken", type: "password", title: "Optional API Token (Bearer)", required: false
        input name: "debugLogging", type: "bool", title: "Enable debug logging", defaultValue: false
    }
}

/* ================= Lifecycle ================= */

def installed() {
    logInfo "Installed"
    ensureState(true)
    unschedule()
    runEvery1Minute("connectionWatchdog")
    runIn(2, "Connect")
}

def updated() {
    logInfo "Updated"
    ensureState(false)
    unschedule()
    runEvery1Minute("connectionWatchdog")
    // Do not force-connect here; watchdog + user Initialize/Connect controls that.
}

def initialize() {
    logInfo "initialize()"
    ensureState(false)
    Connect()
}

/**
 * Ensures required state keys exist.
 * If resetUi is true, also resets UI attributes to sane defaults.
 *
 * This MUST be safe to call from async callbacks (webSocketStatus/parse/watchdog).
 */
private ensureState(boolean resetUi = false) {
    if (state.pending == null) state.pending = [:]
    if (state.manualDisconnect == null) state.manualDisconnect = false
    if (state.connecting == null) state.connecting = false
    if (state.socketOpen == null) state.socketOpen = false

    if (resetUi) {
        sendEvent(name: "is_connected", value: false)
        sendEvent(name: "connection_status", value: "starting")
    } else {
        if (device.currentValue("is_connected") == null) {
            sendEvent(name: "is_connected", value: false)
        }
        if (!device.currentValue("connection_status")) {
            sendEvent(name: "connection_status", value: "starting")
        }
    }
}

/* ================= Watchdog ================= */

def connectionWatchdog() {
    ensureState(false)

    logDebug "Watchdog: open=${state.socketOpen} connecting=${state.connecting} manual=${state.manualDisconnect}"

    if (state.manualDisconnect) return
    if (state.socketOpen) return
    if (state.connecting) return

    logWarn "Watchdog: not connected; attempting Connect()"
    Connect()
}

/* ================= WebSocket ================= */

def Connect() {
    ensureState(false)

    if (state.manualDisconnect) {
        logInfo "Connect(): clearing manualDisconnect"
        state.manualDisconnect = false
    }

    if (state.socketOpen) {
        logInfo "Connect() ignored; already open"
        return
    }

    if (state.connecting) {
        logInfo "Connect() ignored; already connecting"
        return
    }

    state.connecting = true
    sendEvent(name: "connection_status", value: "connecting")

    String uri = "ws://${wsHost}:${wsPort}"
    Map options = [ pingInterval: 30 ]

    if (apiToken?.trim()) {
        options.headers = ["Authorization": "Bearer ${apiToken.trim()}"]
    }

    logInfo "Connecting to ${uri}"
    try {
        interfaces.webSocket.connect(options, uri)
    } catch (e) {
        state.connecting = false
        state.socketOpen = false
        sendEvent(name: "is_connected", value: false)
        sendEvent(name: "connection_status", value: "connect_exception")
        logWarn "connect() threw: ${e}"
    }
}

def Disconnect() {
    ensureState(false)

    logInfo "Disconnecting (manual)"
    state.manualDisconnect = true

    // Stop treating as connected immediately (UI should reflect intent)
    state.connecting = false
    state.socketOpen = false
    state.pending = [:]

    sendEvent(name: "is_connected", value: false)
    sendEvent(name: "connection_status", value: "disconnected")

    try {
        interfaces.webSocket.close()
    } catch (e) {
        logDebug "WS close ignored: ${e}"
    }
}

def webSocketStatus(String status) {
    ensureState(false)
    logDebug "WS status: ${status}"

    // Hubitat reports strings like: "status: open", "status: closing", "failure: ..."
    if (status?.contains("open")) {
        state.connecting = false
        state.socketOpen = true

        sendEvent(name: "is_connected", value: true)
        sendEvent(name: "connection_status", value: "connected")

        // Kick off discovery once connected
        listLocks()
        return
    }

    // Anything else means not open
    state.connecting = false
    state.socketOpen = false

    sendEvent(name: "is_connected", value: false)
    sendEvent(name: "connection_status", value: status ?: "disconnected")
}

/* ================= Parsing ================= */

def parse(String msg) {
    ensureState(false)
    logDebug "WS recv: ${msg}"

    def json
    try {
        json = new JsonSlurper().parseText(msg)
    } catch (e) {
        logWarn "Invalid JSON: ${e}"
        return
    }

    if (json?.type == "event" && json?.event == "lock_state") {
        ensureChild(json.lock_name)?.applySnapshot(json.state)
        return
    }

    if (json?.type == "response") {
        handleResponse(json)
        return
    }
}

/* ================= Responses ================= */

private handleResponse(resp) {
    ensureState(false)

    if (resp?.status == "error") {
        logWarn "WS error: request_id=${resp.request_id} error=${resp.error}"
        state.pending?.remove(resp.request_id)
        return
    }

    def meta = state.pending?.remove(resp.request_id)
    if (!meta) {
        // Could be late/duplicate response after a reboot or after manual disconnect
        logDebug "Ignoring response for unknown request_id=${resp.request_id}"
        return
    }

    if (meta.kind == "list_locks") {
        resp.data?.locks?.each { ensureChild(it) }
        refreshAll()
        return
    }

    if (meta.kind == "get_state") {
        getChildByLockName(meta.lockName)?.applySnapshot(resp.data)
        return
    }
}

/* ================= Commands ================= */

def refresh() {
    refreshAll()
}

private listLocks() {
    sendCmd("list_locks", null, "list_locks")
}

private refreshAll() {
    getChildDevices().each { cd ->
        def ln = cd.getDataValue("lock_name")
        if (ln) sendCmd("get_state", ln, "get_state", [lockName: ln])
    }
}

def childLock(String lockName)   { sendCmd("lock",   lockName, "lock") }
def childUnlock(String lockName) { sendCmd("unlock", lockName, "unlock") }
def childGetState(String lockName) {
    sendCmd("get_state", lockName, "get_state", [lockName: lockName])
}

private sendCmd(String command, String lockName, String kind, Map meta = [:]) {
    ensureState(false)

    // Gate on the state flags (not on attributes which can lag)
    if (!state.socketOpen) {
        logWarn "WS not open; dropping ${command}"
        return
    }

    def reqId = UUID.randomUUID().toString()
    state.pending[reqId] = ([kind: kind] + meta)

    def payload = [
        type: "command",
        request_id: reqId,
        command: command,
        lock_name: lockName
    ].findAll { it.value != null }

    def json = JsonOutput.toJson(payload)
    logDebug "WS send: ${json}"
    interfaces.webSocket.sendMessage(json)
}

/* ================= Child Devices ================= */

private ensureChild(String lockName) {
    if (!lockName) return null

    def dni = "${device.id}:${lockName}"
    def child = getChildDevice(dni)
    if (child) return child

    logInfo "Creating child device: ${lockName}"
    child = addChildDevice(
        "k-mtg",
        "August BLE Lock (Child)",
        dni,
        [label: lockName, isComponent: true]
    )
    child.updateDataValue("lock_name", lockName)
    return child
}

private getChildByLockName(String lockName) {
    if (!lockName) return null
    getChildDevice("${device.id}:${lockName}")
}

/* ================= Logging ================= */

private logDebug(msg) { if (debugLogging) log.debug "${device.displayName}: ${msg}" }
private logInfo(msg)  { log.info  "${device.displayName}: ${msg}" }
private logWarn(msg)  { log.warn  "${device.displayName}: ${msg}" }
