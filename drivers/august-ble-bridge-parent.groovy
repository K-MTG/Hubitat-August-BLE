/**
 *  August BLE Bridge (Parent)
 *
 *  Filename: august-ble-bridge-parent.groovy
 *  Version:  0.1.0
 *
 *  Description:
 *  - Maintains a persistent WebSocket connection to an August BLE service
 *  - Discovers locks via list_locks
 *  - Creates and manages child lock devices
 *  - Routes WS events to child devices
 *  - Routes child commands to the WS service
 *
 *  Requirements:
 *  - Hubitat Elevation
 *  - External August BLE WebSocket service
 *
 *  Changelog:
 *  0.1.0 (2025-12-14)
 *   - Initial public release
 *   - Stable WebSocket lifecycle handling
 *   - Auth header support
 *   - Automatic reconnect with backoff
 */

import groovy.json.JsonOutput
import groovy.json.JsonSlurper

metadata {
    definition(name: "August BLE Bridge (Parent)", namespace: "k-mtg", author: "K-MTG", importUrl: "https://raw.githubusercontent.com/K-MTG/Hubitat-August-BLE/refs/heads/main/drivers/august-ble-bridge-parent.groovy") {
        capability "Initialize"
        capability "Refresh"

        command "Connect"
        command "Disconnect"

        attribute "is_connected", "bool"
        attribute "connection_status", "string"
        attribute "last_error", "string"
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
    state.pending = [:]
    state.reconnectDelay = 2
    state.manualDisconnect = false
    state.wsState = "DISCONNECTED"
    sendEvent(name: "is_connected", value: false)
}

def updated() {
    logInfo "Updated"
    if (debugLogging) runIn(1800, "logsOff")
}

def initialize() {
    logInfo "initialize()"
    Connect()
}


def logsOff() {
    device.updateSetting("debugLogging", [value: "false", type: "bool"])
    logInfo "Debug logging disabled"
}

/* ================= WebSocket ================= */

def Connect() {
    if (state.wsState in ["CONNECTING", "CONNECTED"]) {
        logInfo "Connect() ignored; state=${state.wsState}"
        return
    }

    state.manualDisconnect = false
    state.wsState = "CONNECTING"

    String uri = "ws://${wsHost}:${wsPort}".toString()

    Map options = [ pingInterval: 30 ]
    if (apiToken?.trim()) {
        options.headers = [
            "Authorization": "Bearer ${apiToken.trim()}"
        ]
    }

    logInfo "Connecting to ${uri}"
    interfaces.webSocket.connect(options, uri)
}

def Disconnect() {
    logInfo "Disconnecting (manual)"

    state.manualDisconnect = true
    unschedule("Connect")
    state.pending?.clear()

    state.wsState = "DISCONNECTED"

    sendEvent(name: "is_connected", value: false)
    sendEvent(name: "connection_status", value: "disconnected")

    try {
        interfaces.webSocket.close()
    } catch (e) {
        logDebug "WS close ignored: ${e}"
    }
}


def webSocketStatus(String status) {
    logDebug "WS status: ${status}"

    if (status.contains("open")) {
        state.wsState = "CONNECTED"
        state.manualDisconnect = false

        sendEvent(name: "is_connected", value: true)
        sendEvent(name: "connection_status", value: "connected")

        state.reconnectDelay = 2
        listLocks()
        return
    }

    if (status.contains("closing")) {
        state.wsState = "DISCONNECTED"

        sendEvent(name: "is_connected", value: false)
        sendEvent(name: "connection_status", value: "closing")

        if (!state.manualDisconnect) {
            logInfo "Socket closed unexpectedly; scheduling reconnect"
            scheduleReconnect()
        } else {
            logDebug "Closing due to manual disconnect; no reconnect"
        }
        return
    }

    if (status.contains("failure") || status.contains("closed")) {
        state.wsState = "DISCONNECTED"

        sendEvent(name: "is_connected", value: false)
        sendEvent(name: "connection_status", value: status)

        if (!state.manualDisconnect) {
            scheduleReconnect()
        } else {
            logDebug "Reconnect suppressed (manual disconnect)"
        }
    }
}


def parse(String msg) {
    logDebug "WS recv: ${msg}"

    def json
    try {
        json = new JsonSlurper().parseText(msg)
    } catch (e) {
        logWarn "Invalid JSON: ${e}"
        return
    }

    if (json.type == "event" && json.event == "lock_state") {
        handleLockEvent(json)
    } else if (json.type == "response") {
        handleResponse(json)
    }
}

/* ================= Message Handling ================= */

private handleLockEvent(evt) {
    def child = ensureChild(evt.lock_name)
    child?.applySnapshot(evt.state)
}

private handleResponse(resp) {
    def meta = state.pending.remove(resp.request_id)
    if (!meta || resp.status != "ok") {
        if (resp?.error) logWarn "WS error: ${resp.error}"
        return
    }

    if (meta.kind == "list_locks") {
        resp.data.locks.each { ensureChild(it) }
        refreshAll()
    }

    if (meta.kind == "get_state") {
        getChildByLockName(meta.lockName)?.applySnapshot(resp.data)
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
        sendCmd(
            "get_state",
            cd.getDataValue("lock_name"),
            "get_state",
            [lockName: cd.getDataValue("lock_name")]
        )
    }
}

def childLock(String lockName) {
    sendCmd("lock", lockName, "lock")
}

def childUnlock(String lockName) {
    sendCmd("unlock", lockName, "unlock")
}

def childGetState(String lockName) {
    sendCmd("get_state", lockName, "get_state", [lockName: lockName])
}

private sendCmd(String command, String lockName, String kind, Map meta = [:]) {
    if (state.wsState != "CONNECTED") {
        logWarn "WS not connected; dropping command ${command}"
        return
    }

    def reqId = UUID.randomUUID().toString()
    state.pending[reqId] = [kind: kind] + meta

    def payload = [
        type: "command",
        request_id: reqId,
        command: command
    ]
    if (lockName) payload.lock_name = lockName

    def json = JsonOutput.toJson(payload)
    logDebug "WS send: ${json}"
    interfaces.webSocket.sendMessage(json)
}

/* ================= Child Devices ================= */

private ensureChild(String lockName) {
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
    getChildDevice("${device.id}:${lockName}")
}

/* ================= Reconnect ================= */

private scheduleReconnect() {
    unschedule("Connect")

    logInfo "Scheduling reconnect in ${state.reconnectDelay}s"
    runIn(state.reconnectDelay, "Connect")

    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 60)
}


/* ================= Logging ================= */

private logDebug(msg) { if (debugLogging) log.debug "${device.displayName}: ${msg}" }
private logInfo(msg)  { log.info  "${device.displayName}: ${msg}" }
private logWarn(msg)  { log.warn  "${device.displayName}: ${msg}" }
