import QtQuick
import Quickshell
import Quickshell.Io

// Headless state for the Tether widget. Polls backend.py, which reads
// tetherd's state_snapshot over its Unix socket, and runs actions (pairing,
// sending, clipboard) through the same helper. The daemon itself is owned by
// the tetherd.service user unit; the power switch starts and stops that unit.
Item {
  id: root

  property var settings: ({})

  readonly property string backendPath: String(Qt.resolvedUrl("backend.py")).replace(/^file:\/\//, "")

  property bool installed: true
  property bool running: false
  property string unit: "unknown"
  property bool clipboardSync: false
  property bool mdns: false
  property var devices: []
  property var pending: []
  property var received: []
  property var bt: null
  property var threads: []
  property int unread: 0
  property var calls: []
  property bool loaded: false
  property string actionStatus: ""
  property string lastError: ""

  // -1 follows the real state; 0/1 while a start/stop is still settling, so
  // the switch throws the instant it is clicked.
  property int _desired: -1
  readonly property bool active: _desired === -1 ? running : (_desired === 1)

  readonly property var connectedDevices: devices.filter(function(d) { return d.connected })
  readonly property bool connected: connectedDevices.length > 0
  // The name set on the phone, from Bluetooth; the Wi-Fi pairing name otherwise.
  property string btPhoneName: ""
  readonly property string phoneName: btPhoneName !== "" ? btPhoneName
    : (connected ? String(connectedDevices[0].name) : (devices.length > 0 ? String(devices[0].name) : ""))
  readonly property bool busy: controlProcess.running || actionProcess.running

  // Bluetooth side: messages (MAP) and call control (HFP via BlueZ Telephony).
  readonly property bool messagesReady: !!bt && bt.messages === true
  readonly property bool callsReady: !!bt && bt.calls_ready === true
  readonly property var ringingCall: {
    for (var i = 0; i < calls.length; i++) if (calls[i].ringing && !calls[i].outgoing) return calls[i]
    return null
  }
  readonly property bool inCall: calls.length > 0
  readonly property bool ringing: ringingCall !== null
  readonly property bool hasPending: pending.length > 0
  // Reachable at all: over Wi-Fi (Tether app) or over Bluetooth (MAP/HFP).
  readonly property bool phoneHere: connected || (!!bt && bt.present === true)
  readonly property int battery: bt ? Number(bt.battery || 0) : 0

  // Open conversation and dialer state, filled on demand by the panel.
  property string openThread: ""
  property var conversation: []
  property bool conversationLoading: false
  property var contactResults: []
  property var history: []
  property string _ringingNotified: ""

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 10, 3, 600)
  readonly property bool notifyPairing: setting("notifyPairing", true) !== false

  property var _notifiedPending: ({})
  property string _statusOut: ""
  property string _actionOut: ""
  property string _controlErr: ""

  signal pendingArrived(var request)

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function refresh() {
    if (statusProcess.running) return
    _statusOut = ""
    statusProcess.command = ["python3", backendPath, "status"]
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var s
    try { s = JSON.parse(raw) } catch (e) { lastError = "Could not read Tether status"; return }
    installed = s.installed === true
    unit = String(s.unit || "unknown")
    running = s.running === true
    if (_desired !== -1 && running === (_desired === 1)) _desired = -1
    clipboardSync = s.clipboard === true
    mdns = s.mdns === true
    devices = s.devices || []
    received = s.received || []
    bt = s.bt || null
    threads = s.threads || []
    unread = Number(s.unread || 0)
    calls = s.calls || []
    btPhoneName = String(s.phone_name || "")
    lastError = String(s.error || "")

    var ring = ringingCall
    if (ring && _ringingNotified !== ring.path) {
      _ringingNotified = ring.path
      notifyIncomingCall(ring)
    } else if (!ring) {
      _ringingNotified = ""
      if (callNotifyProcess.running) callNotifyProcess.running = false
    }
    loaded = true

    var next = s.pending || []
    var seen = {}
    for (var i = 0; i < next.length; i++) {
      var fp = String(next[i].fingerprint || "")
      seen[fp] = true
      if (!_notifiedPending[fp]) pendingArrived(next[i])
    }
    _notifiedPending = seen
    pending = next
  }

  // The bar creates this widget once per monitor, so every copy sees the same
  // event. A shared non-blocking lock lets only the first copy notify; it is
  // held a few seconds (or, for a call, until the notification is answered).
  function onceCommand(key, args, holdSeconds) {
    var lock = (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/io.github.sgatter.omatinuity-" + String(key).replace(/[^A-Za-z0-9]+/g, "_") + ".lock"
    // -o: only flock holds the lock, so stopping it (call ended) frees it.
    return ["flock", "-n", "-o", lock, "sh", "-c", '"$@"; s=$?; sleep ' + holdSeconds + '; exit $s', "sh"].concat(args)
  }

  function notify(title, body) {
    Quickshell.execDetached(onceCommand("notify-" + title, ["notify-send", "-a", "Tether", "-i", "/usr/share/icons/Adwaita/symbolic/devices/phone-symbolic.svg", title, body], 5))
  }

  function runAction(args, okRefresh) {
    if (actionProcess.running) return false
    _actionOut = ""
    actionProcess.command = ["python3", backendPath].concat(args)
    actionProcess.running = true
    return true
  }

  function accept(fingerprint) { return runAction(["accept", String(fingerprint)]) }
  function forget(fingerprint) { return runAction(["forget", String(fingerprint)]) }
  function sendClipboardText() { return runAction(["clipboard"]) }
  function sendCopiedFiles() { return runAction(["send-clipboard"]) }
  function sendScreenshot() { return runAction(["send-screenshot"]) }
  function sendFiles(paths) { return runAction(["send"].concat(paths)) }

  function callLabel(call) {
    if (!call) return ""
    if (call.name) return String(call.name)
    return call.withheld || !call.number ? "Unknown caller" : String(call.number)
  }

  function notifyIncomingCall(call) {
    if (callNotifyProcess.running) callNotifyProcess.running = false
    callNotifyProcess.callPath = String(call.path)
    callNotifyProcess.command = onceCommand("call-" + call.path, ["notify-send", "-a", "Tether", "-i", "/usr/share/icons/Adwaita/symbolic/actions/call-start-symbolic.svg", "-u", "critical",
      "-A", "answer=Answer", "-A", "hangup=Decline", "--wait",
      "Incoming call", callLabel(call) + (call.name && call.number ? " · " + call.number : "")], 0)
    callNotifyProcess.running = true
  }

  function answer(path) { return runAction(["answer", String(path || "")]) }
  function hangup(path) { return runAction(["hangup", String(path || "")]) }
  function dial(number) {
    var n = String(number || "").trim()
    if (n === "") { flash("Enter a number"); return false }
    return runAction(["dial", n])
  }

  function openConversation(thread) {
    openThread = String(thread || "")
    conversation = []
    loadConversation()
  }

  function closeConversation() {
    openThread = ""
    conversation = []
  }

  function loadConversation() {
    if (openThread === "" || conversationProcess.running) return
    conversationLoading = true
    conversationProcess.command = ["python3", backendPath, "messages", openThread]
    conversationProcess.running = true
  }

  function sendReply(body) {
    var text = String(body || "")
    if (openThread === "" || text.trim() === "") return false
    if (!runAction(["reply", openThread, text])) return false
    // Show it right away; the reload after the send replaces it with the real one.
    var next = conversation.slice()
    next.push({ body: text, outgoing: true, timestamp: Math.floor(Date.now() / 1000), pending: true })
    conversation = next
    return true
  }

  function searchContacts(query) {
    contactProcess.running = false
    contactProcess.command = ["python3", backendPath, "contacts", String(query || "")]
    contactProcess.running = true
  }

  function loadHistory() {
    if (historyProcess.running) return
    historyProcess.command = ["python3", backendPath, "history"]
    historyProcess.running = true
  }

  function threadFor(thread) {
    for (var i = 0; i < threads.length; i++) if (threads[i].thread === thread) return threads[i]
    return null
  }

  function setRunning(on) {
    if (!installed || controlProcess.running) return
    _desired = on ? 1 : 0
    _controlErr = ""
    controlProcess.command = ["systemctl", "--user", on ? "start" : "stop", "tetherd.service"]
    controlProcess.running = true
  }

  function toggleRunning() { setRunning(!active) }

  function openFile(file) {
    if (!file || !file.path) return
    var parts = String(file.path).split("/")
    for (var i = 0; i < parts.length; i++) parts[i] = encodeURIComponent(parts[i])
    Quickshell.execDetached(["uwsm-app", "--", "nautilus", "--select", "file://" + parts.join("/")])
  }

  function openDownloads() {
    Quickshell.execDetached(["sh", "-c", "uwsm-app -- nautilus \"$(xdg-user-dir DOWNLOAD)\""])
  }

  function openApp() {
    Quickshell.execDetached(["uwsm-app", "--", "tether-gtk"])
  }

  function flash(text) {
    actionStatus = text
    actionStatusTimer.restart()
  }

  onPendingArrived: function(request) {
    if (notifyPairing)
      notify("iPhone wants to pair", String(request.name || "A device") + " is asking to pair with Tether. Open the bar widget to accept.")
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    // A live call changes state (ringing → active → gone) without changing the
    // call count the daemon broadcasts on, so poll fast while one exists.
    interval: 1000
    repeat: true
    running: root.inCall
    onTriggered: root.refresh()
  }

  Timer {
    // Keep one subscriber attached to tetherd; it dies with the daemon.
    interval: 3000
    repeat: true
    running: root.running && !watchProcess.running
    triggeredOnStart: true
    onTriggered: watchProcess.running = true
  }

  Timer {
    // Coalesce bursts of events into one status read.
    id: eventRefresh
    interval: 250
    onTriggered: {
      root.refresh()
      if (root.openThread !== "") root.loadConversation()
    }
  }

  Process {
    id: watchProcess
    command: ["python3", root.backendPath, "watch"]
    stdout: SplitParser {
      onRead: function(line) {
        if (String(line) !== "ready") eventRefresh.restart()
      }
    }
  }

  Process {
    id: callNotifyProcess
    property string callPath: ""
    stdout: SplitParser {
      onRead: function(line) {
        var choice = String(line).trim()
        if (choice === "answer") root.answer(callNotifyProcess.callPath)
        else if (choice === "hangup") root.hangup(callNotifyProcess.callPath)
      }
    }
  }

  Process {
    id: conversationProcess
    stdout: StdioCollector { id: conversationStdout; waitForEnd: true }
    onExited: function(exitCode) {
      root.conversationLoading = false
      var r = {}
      try { r = JSON.parse(String(conversationStdout.text || "{}")) } catch (e) { r = {} }
      if (r.ok && r.thread === root.openThread) root.conversation = r.messages || []
      else if (!r.ok) root.flash(String(r.message || "Could not load the conversation"))
    }
  }

  Process {
    id: contactProcess
    stdout: StdioCollector { id: contactStdout; waitForEnd: true }
    onExited: function(exitCode) {
      var r = {}
      try { r = JSON.parse(String(contactStdout.text || "{}")) } catch (e) { r = {} }
      root.contactResults = r.ok ? (r.contacts || []) : []
    }
  }

  Process {
    id: historyProcess
    stdout: StdioCollector { id: historyStdout; waitForEnd: true }
    onExited: function(exitCode) {
      var r = {}
      try { r = JSON.parse(String(historyStdout.text || "{}")) } catch (e) { r = {} }
      root.history = r.ok ? (r.calls || []) : []
    }
  }

  Timer {
    // tetherd takes a moment to open its socket after start (and to drop it
    // after stop), so re-poll a few times instead of waiting a full interval.
    id: settleTimer
    property int ticks: 0
    interval: 1000
    repeat: true
    onTriggered: {
      ticks += 1
      root.refresh()
      if (ticks >= 5 || root._desired === -1) {
        ticks = 0
        running = false
        root._desired = -1
      }
    }
  }

  Timer {
    id: actionStatusTimer
    interval: 2600
    onTriggered: root.actionStatus = ""
  }

  Process {
    id: statusProcess
    stdout: StdioCollector { id: statusStdout; waitForEnd: true; onStreamFinished: root._statusOut = text }
    onExited: function(exitCode) {
      var out = String(statusStdout.text || root._statusOut || "")
      if (exitCode === 0 && out !== "") root.applyStatus(out)
      else root.lastError = "Tether status helper failed"
    }
  }

  Process {
    id: actionProcess
    stdout: StdioCollector { id: actionStdout; waitForEnd: true; onStreamFinished: root._actionOut = text }
    onExited: function(exitCode) {
      var r = {}
      try { r = JSON.parse(String(actionStdout.text || root._actionOut || "{}")) } catch (e) { r = { ok: false, message: "Tether action failed" } }
      root.flash(String(r.message || (r.ok ? "Done" : "Failed")))
      root.refresh()
      if (root.openThread !== "") root.loadConversation()
    }
  }

  Process {
    id: controlProcess
    stderr: StdioCollector { id: controlStderr; waitForEnd: true; onStreamFinished: root._controlErr = text }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root._desired = -1
        root.flash(String(controlStderr.text || root._controlErr || "Could not change tetherd.service").trim())
      }
      settleTimer.ticks = 0
      settleTimer.restart()
    }
  }
}
