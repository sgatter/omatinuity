import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.sgatter.omatinuity"
  manageIpc: false

  // Injected by BarWidget.qml, which owns the button, the Tether service and
  // the shell IPC; this file is only the details panel.
  property var anchorItem: null
  property var hostWidget: null
  property QtObject tether: null

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  // "main" is the overview; "thread" an open conversation; "dial" the
  // call-or-message picker. Esc in a sub-view returns to main.
  property string view: "main"

  // Keyboard cursor: "header" is the power switch; "rows" walks one flat list
  // of every actionable row in the current view.
  property string focusSection: "header"
  property int rowIndex: 0
  property bool cursorActive: false
  property int phraseIndex: 0

  readonly property var activePhrases: [
    "Bridging bytes",
    "Pocketing packets",
    "Ferrying files",
    "Relaying ripples",
    "Courting the clipboard",
    "Whispering to Bonjour",
    "Pinning certificates",
    "Holding hands over Wi-Fi"
  ]

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string phoneGlyph: glyph(0xF011C)
  readonly property bool hasPending: tether.hasPending
  readonly property bool ringing: tether.ringing
  readonly property bool btSetUp: !!tether.bt && (tether.bt.paired || tether.bt.present || tether.threads.length > 0)
  readonly property bool btSetupNeeded: !!tether.bt && tether.bt.setup_needed === true
  readonly property bool phoneHere: tether.phoneHere

  readonly property string heroMeta: {
    if (!tether.installed) return "Not installed"
    if (!tether.active) return "Daemon stopped"
    if (phoneHere) return activePhrases[phraseIndex % activePhrases.length]
    if (!tether.mdns) return "mDNS unavailable"
    if (tether.devices.length > 0) return "Waiting for " + tether.phoneName
    return "No iPhone paired yet"
  }
  readonly property string heroDetail: {
    if (!tether.installed || !tether.active || !phoneHere) return ""
    return tether.battery > 0 ? tether.phoneName + " · " + tether.battery + "%" : tether.phoneName
  }

  readonly property string setupScript: String(Qt.resolvedUrl("setup-bluetooth.sh")).replace(/^file:\/\//, "")

  // Actions are static rows; whether each is usable depends on daemon state.
  readonly property var actions: [
    { id: "clip", glyph: 0xF018F, title: "Send clipboard", detail: "Push the current clipboard text to the iPhone" },
    { id: "files", glyph: 0xF0214, title: "Send copied files", detail: "Copy files in Nautilus (Ctrl+C), then send them" },
    { id: "shot", glyph: 0xF0E51, title: "Send latest screenshot", detail: "The newest image in your screenshots folder" },
    { id: "downloads", glyph: 0xF024D, title: "Open Downloads", detail: "Where files from the iPhone land" },
    { id: "app", glyph: 0xF03CC, title: "Open Tether", detail: "Notifications, settings and the full app" }
  ]

  // Flat list the keyboard cursor walks, in on-screen order, per view.
  readonly property var rows: {
    var out = []
    var i
    if (view === "dial") {
      if (dialNumber !== "") out.push({ kind: "dialnumber", data: { number: dialNumber } })
      var list = dialQuery === "" ? tether.history : tether.contactResults
      for (i = 0; i < list.length; i++) out.push({ kind: dialQuery === "" ? "history" : "contact", data: list[i] })
      return out
    }
    if (view === "thread") return out
    for (i = 0; i < tether.calls.length; i++) out.push({ kind: "call", data: tether.calls[i] })
    for (i = 0; i < tether.pending.length; i++) out.push({ kind: "pending", data: tether.pending[i] })
    if (tether.active && btSetUp) {
      out.push({ kind: "compose", data: null })
      for (i = 0; i < tether.threads.length; i++) out.push({ kind: "thread", data: tether.threads[i] })
    }
    if (tether.active && (!btSetUp || btSetupNeeded)) out.push({ kind: "setup", data: null })
    if (tether.active) for (i = 0; i < actions.length; i++) out.push({ kind: "action", data: actions[i] })
    for (i = 0; i < tether.devices.length; i++) out.push({ kind: "device", data: tether.devices[i] })
    for (i = 0; i < tether.received.length; i++) out.push({ kind: "received", data: tether.received[i] })
    return out
  }

  // Dialer input: the raw query, and a dialable number when it looks like one.
  property string dialQuery: ""
  readonly property string dialNumber: /^[+0-9][0-9 ()\-.]{2,}$/.test(dialQuery.trim()) ? dialQuery.trim().replace(/[ ()\-.]/g, "") : ""

  readonly property var openThreadInfo: tether.threadFor(tether.openThread)
  readonly property string openThreadName: openThreadInfo ? String(openThreadInfo.name) : threadTitleOverride
  property string threadTitleOverride: ""

  function glyph(cp) { return String.fromCodePoint(cp) }

  function rowOffset(kind) {
    for (var i = 0; i < rows.length; i++) if (rows[i].kind === kind) return i
    return 0
  }

  function rowHasCursor(index) {
    return cursorActive && focusSection === "rows" && rowIndex === index
  }

  function setRowCursor(index) {
    cursorActive = true
    focusSection = "rows"
    rowIndex = index
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    if (dy === 0) return
    if (focusSection === "header") {
      if (dy > 0 && rows.length > 0) { focusSection = "rows"; rowIndex = 0 }
      return
    }
    if (dy < 0 && rowIndex === 0) {
      if (view === "dial") { focusSection = "header"; dialField.forceActiveFocus(); return }
      focusSection = "header"
      panelFlick.contentY = 0
      return
    }
    rowIndex = Math.max(0, Math.min(rows.length - 1, rowIndex + dy))
    scrollCursorIntoView()
  }

  function activateCursor() {
    if (focusSection === "header") {
      if (view === "main") tether.toggleRunning()
      return
    }
    var row = rows[rowIndex]
    if (row) activateRow(row.kind, row.data)
  }

  function activateRow(kind, data) {
    if (kind === "pending") tether.accept(data.fingerprint)
    else if (kind === "received") tether.openFile(data)
    else if (kind === "action") runAction(data.id)
    else if (kind === "call") data.ringing && !data.outgoing ? tether.answer(data.path) : tether.hangup(data.path)
    else if (kind === "thread") openConversation(data.thread, data.name)
    else if (kind === "compose") openDialer()
    else if (kind === "setup") runSetup()
    else if (kind === "dialnumber") tether.dial(data.number)
    else if (kind === "history") tether.dial(data.number)
    else if (kind === "contact") {
      if (data.number && tether.callsReady) tether.dial(data.number)
      else openConversation(data.address, data.name)
    }
  }

  function runAction(id) {
    if (id === "clip") tether.sendClipboardText()
    else if (id === "files") tether.sendCopiedFiles()
    else if (id === "shot") tether.sendScreenshot()
    else if (id === "downloads") { tether.openDownloads(); root.close() }
    else if (id === "app") { tether.openApp(); root.close() }
  }

  function runSetup() {
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", "'" + setupScript + "'"])
    root.close()
  }

  function openConversation(thread, name) {
    threadTitleOverride = String(name || thread || "")
    tether.openConversation(thread)
    followLatest = true
    view = "thread"
    cursorActive = false
    scrollToBottom()
    Qt.callLater(function() { replyField.forceActiveFocus() })
  }

  function openDialer() {
    dialQuery = ""
    dialField.text = ""
    tether.contactResults = []
    tether.loadHistory()
    view = "dial"
    cursorActive = false
    focusSection = "header"
    Qt.callLater(function() { dialField.forceActiveFocus() })
  }

  function backToMain() {
    if (view === "thread") tether.closeConversation()
    followLatest = false
    view = "main"
    cursorActive = false
    focusSection = "header"
    rowIndex = 0
    panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  // wl-copy rather than the TextEdit's own copy(): the panel is a layer
  // surface, and wl-copy keeps serving the selection after it closes.
  function copyText(text) {
    var t = String(text || "")
    if (t === "") return
    Quickshell.execDetached(["wl-copy", "--", t])
    tether.flash("Copied")
  }

  function sendReply() {
    if (!tether.sendReply(replyField.text)) return
    replyField.text = ""
    followLatest = true
    scrollToBottom()
  }

  function dialFromField() {
    if (dialNumber !== "") tether.dial(dialNumber)
    else if (rows.length > 0) activateRow(rows[0].kind, rows[0].data)
  }

  function scrollCursorIntoView() {
    Qt.callLater(function() {
      var item = rowItems[rowIndex]
      if (!item || !panelFlick) return
      var margin = Style.space(6)
      var top = item.mapToItem(panelFlick.contentItem, 0, 0).y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  // An open conversation stays on its latest message while the bubbles lay
  // out, messages load and the panel grows. Scrolling up lets go; scrolling
  // back to the end picks it up again.
  property bool followLatest: false

  function scrollToBottom() {
    if (panelFlick) panelFlick.contentY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
  }

  // Row delegates register themselves here by flat index for scrolling.
  property var rowItems: ({})

  function formatBytes(n) {
    n = Number(n || 0)
    if (n < 1024) return n + " B"
    var units = ["KB", "MB", "GB"]
    var i = -1
    do { n /= 1024; i++ } while (n >= 1024 && i < units.length - 1)
    return n.toFixed(n < 10 ? 1 : 0) + " " + units[i]
  }

  function formatTime(ts) {
    ts = Number(ts || 0)
    if (ts <= 0) return ""
    var d = new Date(ts * 1000)
    var now = new Date()
    var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
    if (d.getTime() >= startOfToday) return Qt.formatTime(d, "HH:mm")
    if (d.getTime() >= startOfToday - 86400000) return "Yesterday"
    if (d.getTime() >= startOfToday - 6 * 86400000) return Qt.formatDate(d, "ddd")
    return Qt.formatDate(d, "d MMM")
  }

  function callStateText(call) {
    if (call.ringing && !call.outgoing) return "Incoming call"
    if (call.outgoing && !call.connected) return "Calling…"
    if (call.state === "held") return "On hold"
    return "In call · audio on the iPhone"
  }

  function historyGlyph(type) {
    if (type === "missed") return glyph(0xF03FA)
    if (type === "dialed" || type === "outgoing") return glyph(0xF03FB)
    return glyph(0xF03F7)
  }

  onOpenedChanged: if (opened) {
    followLatest = false
    view = "main"
    cursorActive = false
    focusSection = (hasPending || ringing) ? "rows" : "header"
    rowIndex = 0
    if (panelFlick) panelFlick.contentY = 0
    tether.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  } else {
    tether.closeConversation()
  }
  onRowsChanged: if (rowIndex >= rows.length) rowIndex = Math.max(0, rows.length - 1)
  onRingingChanged: if (ringing && opened && view === "main") { focusSection = "rows"; rowIndex = 0 }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: replyField.activeFocus || dialField.activeFocus
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.view === "main" ? root.close() : root.backToMain()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (root.view !== "main") return
        var k = String(t).toLowerCase()
        if (k === "r") tether.refresh()
        else if (k === "p") tether.toggleRunning()
        else if (k === "c") tether.sendClipboardText()
        else if (k === "f") tether.sendCopiedFiles()
        else if (k === "s") tether.sendScreenshot()
        else if (k === "n" && root.btSetUp) root.openDialer()
        else if (k === "a" && root.ringing) tether.answer(tether.ringingCall.path)
        else if (k === "d" && tether.inCall) tether.hangup("")
        else if (k === "a" && root.hasPending) tether.accept(tether.pending[0].fingerprint)
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        onContentHeightChanged: if (root.followLatest) root.scrollToBottom()
        onHeightChanged: if (root.followLatest) root.scrollToBottom()
        onContentYChanged: if (root.view === "thread")
          root.followLatest = contentY >= contentHeight - height - Style.space(8)

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ── Main view ──────────────────────────────────────────────────
          Item {
            id: header
            visible: root.view === "main"
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: root.cursorActive && root.focusSection === "header" && tether.installed
            function focusHero() { root.cursorActive = true; root.focusSection = "header" }

            PanelHero {
              id: hero
              width: parent.width
              title: "Tether"
              meta: root.heroMeta
              detail: root.heroDetail
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: root.phoneHere ? 1.0 : 0.5
              iconComponent: Component {
                Text {
                  text: root.phoneGlyph
                  color: root.hasPending || root.ringing ? root.urgent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  id: powerSwitch
                  visible: tether.installed
                  checked: tether.active
                  busy: tether.busy
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function(on) { if (on) header.focusHero() }
                  onToggled: tether.toggleRunning()

                  PanelToolTip {
                    visible: powerSwitch.containsMouse
                    text: tether.active ? "Stop tetherd" : "Start tetherd"
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            text: tether.actionStatus !== "" ? tether.actionStatus : (root.view === "main" ? tether.lastError : "")
            color: tether.actionStatus === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            visible: root.view === "main" && tether.loaded && !tether.installed
            width: parent.width
            text: "Install it with: yay -S tether, then systemctl --user enable --now tetherd"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          // Live calls, above everything else.
          Section {
            visible: root.view === "main" && tether.calls.length > 0
            title: root.ringing ? "INCOMING CALL" : "CALL"
            Repeater {
              model: tether.calls
              ActionRow {
                required property var modelData
                required property int index
                readonly property bool incoming: modelData.ringing && !modelData.outgoing
                flatIndex: root.rowOffset("call") + index
                glyphText: incoming ? root.glyph(0xF11AB) : root.glyph(0xF03F2)
                glyphColor: incoming ? root.urgent : root.foreground
                title: tether.callLabel(modelData)
                detail: root.callStateText(modelData) + (modelData.name && modelData.number ? " · " + modelData.number : "")
                trailingGlyph: incoming ? root.glyph(0xF03F2) : ""
                trailingTip: "Answer (a)"
                secondGlyph: root.glyph(0xF03F5)
                secondTip: incoming ? "Decline (d)" : "Hang up (d)"
                secondColor: root.urgent
                onActivated: incoming ? tether.answer(modelData.path) : tether.hangup(modelData.path)
                onTrailingClicked: tether.answer(modelData.path)
                onSecondClicked: tether.hangup(modelData.path)
              }
            }
          }

          Section {
            visible: root.view === "main" && tether.pending.length > 0
            title: "PAIRING REQUEST"
            Repeater {
              model: tether.pending
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("pending") + index
                glyphText: root.phoneGlyph
                glyphColor: root.urgent
                title: String(modelData.name)
                detail: "Fingerprint " + String(modelData.fingerprint).substring(0, 23) + "…"
                trailingGlyph: root.glyph(0xF012C)
                trailingTip: "Accept"
                onActivated: tether.accept(modelData.fingerprint)
                onTrailingClicked: tether.accept(modelData.fingerprint)
              }
            }
          }

          // Messages over Bluetooth MAP.
          Section {
            visible: root.view === "main" && tether.active && root.btSetUp
            title: tether.unread > 0 ? "MESSAGES · " + tether.unread + " UNREAD" : "MESSAGES"

            ActionRow {
              flatIndex: root.rowOffset("compose")
              glyphText: root.glyph(0xF0653)
              title: "Call or message…"
              detail: tether.callsReady ? "Search contacts or type a number (n)" : "Search contacts (n)"
              onActivated: root.openDialer()
            }

            Text {
              visible: tether.threads.length === 0
              width: parent.width
              text: tether.messagesReady ? "No conversations yet." : root.btSetupNeeded ? "Finish the Bluetooth setup below; the iPhone then offers Messages and Contacts access." : (tether.bt && tether.bt.reason ? tether.bt.reason : "Messages are not connected.")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              horizontalAlignment: Text.AlignHCenter
            }

            Repeater {
              model: tether.threads
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("thread") + index
                glyphText: modelData.unread > 0 ? root.glyph(0xF0369) : root.glyph(0xF036A)
                glyphColor: modelData.unread > 0 ? root.accent : root.foreground
                title: String(modelData.name)
                titleBold: modelData.unread > 0
                detail: String(modelData.preview)
                meta: root.formatTime(modelData.timestamp)
                trailingGlyph: modelData.number !== "" && tether.callsReady ? root.glyph(0xF03F2) : ""
                trailingTip: "Call"
                onActivated: root.openConversation(modelData.thread, modelData.name)
                onTrailingClicked: tether.dial(modelData.number)
              }
            }
          }

          Section {
            visible: root.view === "main" && tether.active && (!root.btSetUp || root.btSetupNeeded)
            title: "MESSAGES & CALLS"
            ActionRow {
              flatIndex: root.rowOffset("setup")
              glyphText: root.glyph(0xF00B3)
              title: "Set up Bluetooth"
              detail: "Runs the one-time setup in a terminal (needs sudo)"
              onActivated: root.runSetup()
            }
          }

          Section {
            visible: root.view === "main" && tether.active
            title: "SEND TO IPHONE"
            Repeater {
              model: root.actions
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("action") + index
                glyphText: root.glyph(modelData.glyph)
                title: modelData.title
                detail: modelData.detail
                enabled: !tether.busy
                onActivated: root.runAction(modelData.id)
              }
            }
          }

          Section {
            visible: root.view === "main" && tether.active
            title: "DEVICES"

            Text {
              visible: tether.devices.length === 0
              width: parent.width
              text: "Open the Tether app on your iPhone on the same Wi-Fi, then accept the request here."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              horizontalAlignment: Text.AlignHCenter
            }

            Repeater {
              model: tether.devices
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("device") + index
                glyphText: root.phoneGlyph
                // The Wi-Fi link is up only while the Tether app is active on
                // the phone; Bluetooth (battery, messages, calls) stays up
                // regardless. With one phone both links are the same device.
                readonly property bool viaBluetooth: tether.devices.length === 1 && !!tether.bt && tether.bt.present === true
                glyphColor: modelData.connected || viaBluetooth ? root.foreground : root.dim
                title: String(modelData.name)
                detail: {
                  var wifi = modelData.connected ? "Wi-Fi " + String(modelData.address).replace(/:\d+$/, "") : ""
                  if (viaBluetooth && wifi !== "") return "Bluetooth · " + wifi
                  if (viaBluetooth) return "Bluetooth · Wi-Fi when the Tether app is open"
                  return wifi !== "" ? wifi : "Offline"
                }
                trailingGlyph: root.glyph(0xF0A7A)
                trailingTip: "Forget device"
                // Forgetting is destructive, so only the trailing button does it.
                onTrailingClicked: tether.forget(modelData.fingerprint)
              }
            }
          }

          Section {
            visible: root.view === "main" && tether.active && tether.received.length > 0
            title: "FROM IPHONE"
            Repeater {
              model: tether.received
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("received") + index
                glyphText: root.glyph(0xF0214)
                title: String(modelData.name)
                detail: modelData.exists ? root.formatBytes(modelData.bytes) : "Moved or deleted"
                enabled: modelData.exists === true
                onActivated: tether.openFile(modelData)
              }
            }
          }

          Column {
            visible: root.view === "main" && tether.active
            width: parent.width
            spacing: Style.spacing.labelGap

            PanelSeparator { foreground: root.foreground }

            InfoPair { label: "Clipboard sync"; value: tether.clipboardSync ? "On" : "Off" }
            InfoPair {
              label: "Messages · Notifications"
              value: !tether.bt || !root.btSetUp ? "Bluetooth not set up"
                : (!tether.bt.present ? "Phone not in range"
                : (tether.bt.messages ? "On" : "Off") + " · " + (tether.bt.notifications ? "On" : "Off"))
            }
            InfoPair {
              visible: root.btSetUp
              label: "Calls"
              value: !tether.bt || !tether.bt.calls_enabled ? "Off (tether bt calls-enable on)"
                : (tether.callsReady ? "Ready" + (tether.bt.operator ? " · " + tether.bt.operator : "") + (tether.bt.signal > 0 ? " · signal " + tether.bt.signal + "/5" : "") : "Waiting for Hands-Free")
            }
          }

          // ── Conversation view ──────────────────────────────────────────
          SubHeader {
            visible: root.view === "thread"
            title: root.openThreadName
            subtitle: tether.openThread.indexOf("tel:") === 0 ? tether.openThread.substring(4) : (root.openThreadInfo && root.openThreadInfo.group ? "Group" : "")
            actionGlyph: tether.callsReady && tether.openThread.indexOf("tel:") === 0 ? root.glyph(0xF03F2) : ""
            actionTip: "Call"
            onActionClicked: tether.dial(tether.openThread.substring(4))
          }

          Text {
            visible: root.view === "thread" && tether.conversation.length === 0
            width: parent.width
            text: tether.conversationLoading ? "Loading…" : "No messages yet. Say hi."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            horizontalAlignment: Text.AlignHCenter
          }

          Column {
            visible: root.view === "thread"
            width: parent.width
            spacing: Style.space(6)

            Repeater {
              model: root.view === "thread" ? tether.conversation : []
              Bubble {
                required property var modelData
                required property int index
                readonly property var prev: index > 0 ? tether.conversation[index - 1] : null
                message: modelData
                // A time stamp only where the conversation pauses for 15 minutes.
                showTime: !prev || Number(modelData.timestamp) - Number(prev.timestamp) > 900
              }
            }
          }

          Text {
            visible: root.view === "thread" && !!root.openThreadInfo && root.openThreadInfo.repliable === false
            width: parent.width
            text: root.openThreadInfo && root.openThreadInfo.reason ? root.openThreadInfo.reason : "Replies to this conversation are not supported."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          RowLayout {
            visible: root.view === "thread" && !(root.openThreadInfo && root.openThreadInfo.repliable === false)
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: replyField
              Layout.fillWidth: true
              foreground: root.foreground
              font.family: root.fontFamily
              placeholderText: "iMessage / SMS — Enter to send, Esc to go back"
              enabled: !tether.busy
              Keys.onReturnPressed: root.sendReply()
              Keys.onEnterPressed: root.sendReply()
              Keys.onEscapePressed: root.backToMain()
            }

            PanelActionButton {
              iconText: root.glyph(0xF048A)
              tooltipText: "Send"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !tether.busy && replyField.text.trim() !== ""
              onClicked: root.sendReply()
            }
          }

          // ── Call-or-message view ───────────────────────────────────────
          SubHeader {
            visible: root.view === "dial"
            title: "Call or message"
            subtitle: tether.callsReady ? "Calls are placed on the iPhone" : "Calling is not available yet"
          }

          TextField {
            id: dialField
            visible: root.view === "dial"
            width: parent.width
            foreground: root.foreground
            font.family: root.fontFamily
            placeholderText: "Name or number"
            hasCursor: root.cursorActive && root.focusSection === "header" && root.view === "dial"
            onTextChanged: {
              root.dialQuery = text
              contactSearch.restart()
            }
            Keys.onReturnPressed: root.dialFromField()
            Keys.onEnterPressed: root.dialFromField()
            Keys.onEscapePressed: root.backToMain()
            Keys.onDownPressed: {
              if (root.rows.length === 0) return
              root.setRowCursor(0)
              keyCatcher.forceActiveFocus()
            }
          }

          Section {
            visible: root.view === "dial"
            title: root.dialQuery === "" ? "RECENT CALLS" : "CONTACTS"

            ActionRow {
              visible: root.dialNumber !== ""
              flatIndex: root.rowOffset("dialnumber")
              glyphText: root.glyph(0xF03F2)
              title: "Call " + root.dialNumber
              detail: tether.callsReady ? "Rings from the iPhone" : "Calls are not available yet"
              enabled: tether.callsReady
              onActivated: tether.dial(root.dialNumber)
            }

            Text {
              visible: (root.dialQuery === "" ? tether.history.length : tether.contactResults.length) === 0 && root.dialNumber === ""
              width: parent.width
              text: root.dialQuery === "" ? "No recent calls." : "No matching contacts."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignHCenter
            }

            Repeater {
              model: root.view === "dial" && root.dialQuery === "" ? tether.history : []
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("history") + index
                glyphText: root.historyGlyph(modelData.type)
                glyphColor: modelData.type === "missed" ? root.urgent : root.foreground
                title: modelData.name || modelData.number || "Unknown"
                detail: (modelData.type ? modelData.type.charAt(0).toUpperCase() + modelData.type.substring(1) : "Call") + (modelData.name && modelData.number ? " · " + modelData.number : "")
                meta: root.formatTime(modelData.timestamp)
                enabled: modelData.number !== "" && tether.callsReady
                trailingGlyph: modelData.number !== "" ? root.glyph(0xF036A) : ""
                trailingTip: "Message"
                onActivated: tether.dial(modelData.number)
                onTrailingClicked: root.openConversation("tel:" + modelData.number, modelData.name || modelData.number)
              }
            }

            Repeater {
              model: root.view === "dial" && root.dialQuery !== "" ? tether.contactResults : []
              ActionRow {
                required property var modelData
                required property int index
                flatIndex: root.rowOffset("contact") + index
                glyphText: root.glyph(0xF0004)
                title: String(modelData.name)
                detail: String(modelData.address).replace(/^(tel|email):/, "")
                trailingGlyph: root.glyph(0xF036A)
                trailingTip: "Message"
                secondGlyph: modelData.number !== "" && tether.callsReady ? root.glyph(0xF03F2) : ""
                secondTip: "Call"
                onActivated: root.activateRow("contact", modelData)
                onTrailingClicked: root.openConversation(modelData.address, modelData.name)
                onSecondClicked: tether.dial(modelData.number)
              }
            }
          }
        }
      }
    }
  }

  Timer {
    id: contactSearch
    interval: 250
    onTriggered: if (root.dialQuery.trim() !== "") tether.searchContacts(root.dialQuery.trim())
  }

  Timer {
    interval: 2800
    running: root.opened && root.phoneHere && root.view === "main"
    repeat: true
    onTriggered: phraseSwap.restart()
  }

  SequentialAnimation {
    id: phraseSwap
    PropertyAnimation { target: hero; property: "metaOpacity"; to: 0.0; duration: 180; easing.type: Easing.OutQuad }
    ScriptAction { script: root.phraseIndex = (root.phraseIndex + 1) % root.activePhrases.length }
    PropertyAnimation { target: hero; property: "metaOpacity"; to: 1.0; duration: 260; easing.type: Easing.InQuad }
  }

  component Section: Column {
    property string title: ""
    default property alias content: body.data
    width: parent.width
    spacing: Style.space(10)

    PanelSectionHeader {
      text: parent.title
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Column {
      id: body
      width: parent.width
      spacing: Style.space(6)
    }
  }

  // Back arrow, a title, and an optional action on the trailing edge.
  component SubHeader: RowLayout {
    id: sub
    property string title: ""
    property string subtitle: ""
    property string actionGlyph: ""
    property string actionTip: ""
    signal actionClicked()

    width: parent.width
    spacing: Style.space(8)

    PanelActionButton {
      iconText: root.glyph(0xF004D)
      tooltipText: "Back (Esc)"
      foreground: root.foreground
      fontFamily: root.fontFamily
      Layout.alignment: Qt.AlignVCenter
      onClicked: root.backToMain()
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.space(1)

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        text: sub.title
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.title
        font.bold: true
        elide: Text.ElideRight
      }

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        visible: text !== ""
        text: sub.subtitle
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    PanelActionButton {
      visible: sub.actionGlyph !== ""
      iconText: sub.actionGlyph
      tooltipText: sub.actionTip
      foreground: root.foreground
      fontFamily: root.fontFamily
      enabled: !root.tether.busy
      Layout.alignment: Qt.AlignVCenter
      onClicked: sub.actionClicked()
    }
  }

  component Bubble: Column {
    id: bubble
    property var message: ({})
    property bool showTime: false
    readonly property bool mine: message.outgoing === true

    width: parent ? parent.width : 0
    spacing: Style.space(4)

    Text {
      visible: bubble.showTime
      width: parent.width
      text: root.formatTime(bubble.message.timestamp)
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      horizontalAlignment: Text.AlignHCenter
    }

    Item {
      width: parent.width
      implicitHeight: body.implicitHeight + Style.space(12)

      Rectangle {
        id: body
        readonly property real maxWidth: bubble.width * 0.78
        anchors.right: bubble.mine ? parent.right : undefined
        anchors.left: bubble.mine ? undefined : parent.left
        // Sized from an unwrapped measurement so the bubble hugs short
        // messages; the selectable TextEdit then wraps inside it.
        width: Math.min(maxWidth, measure.implicitWidth + Style.space(20))
        implicitHeight: bodyText.contentHeight + Style.space(12)
        radius: Style.cornerRadius > 0 ? Style.space(10) : 0
        color: bubble.mine ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.28) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
        border.width: 1
        border.color: bubble.mine ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.55) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)
        opacity: bubble.message.pending ? 0.6 : 1.0

        Text {
          id: measure
          visible: false
          textFormat: Text.PlainText
          text: bodyText.text
          font: bodyText.font
        }

        // Read-only but selectable: drag to select, Ctrl+C copies the
        // selection, right-click copies the whole message.
        TextEdit {
          id: bodyText
          anchors.fill: parent
          anchors.margins: Style.space(6)
          anchors.leftMargin: Style.space(10)
          anchors.rightMargin: Style.space(10)
          textFormat: TextEdit.PlainText
          text: String(bubble.message.body || "")
          color: root.foreground
          selectionColor: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.45)
          selectedTextColor: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: TextEdit.Wrap
          readOnly: true
          selectByMouse: true
          persistentSelection: false
          Keys.onPressed: function(event) {
            if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_C) {
              root.copyText(selectedText !== "" ? selectedText : text)
              event.accepted = true
            } else if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_A) {
              selectAll()
              event.accepted = true
            }
          }

          MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.RightButton
            cursorShape: Qt.IBeamCursor
            onClicked: root.copyText(bodyText.text)
          }
        }
      }
    }
  }

  component ActionRow: CursorSurface {
    id: row
    property int flatIndex: 0
    property string glyphText: ""
    property color glyphColor: root.foreground
    property string title: ""
    property bool titleBold: false
    property string detail: ""
    property string meta: ""
    property string trailingGlyph: ""
    property string trailingTip: ""
    property string secondGlyph: ""
    property string secondTip: ""
    property color secondColor: root.foreground
    signal activated()
    signal trailingClicked()
    signal secondClicked()

    width: parent ? parent.width : 0
    hasCursor: root.rowHasCursor(flatIndex)
    foreground: root.foreground
    opacity: enabled ? 1.0 : 0.5
    implicitHeight: content.implicitHeight + Style.spacing.rowPaddingX

    Component.onCompleted: root.rowItems[flatIndex] = row
    onFlatIndexChanged: root.rowItems[flatIndex] = row

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      enabled: row.enabled
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setRowCursor(row.flatIndex)
      onClicked: row.activated()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: row.glyphText
        color: row.glyphColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: content
        Layout.fillWidth: true
        spacing: Style.space(1)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: row.title
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: row.titleBold
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            text: row.meta
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: text !== ""
          text: row.detail
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      PanelActionButton {
        visible: row.trailingGlyph !== ""
        iconText: row.trailingGlyph
        tooltipText: row.trailingTip
        foreground: root.foreground
        fontFamily: root.fontFamily
        enabled: !root.tether.busy
        Layout.alignment: Qt.AlignVCenter
        onClicked: row.trailingClicked()
      }

      PanelActionButton {
        visible: row.secondGlyph !== ""
        iconText: row.secondGlyph
        tooltipText: row.secondTip
        foreground: row.secondColor
        fontFamily: root.fontFamily
        enabled: !root.tether.busy
        Layout.alignment: Qt.AlignVCenter
        onClicked: row.secondClicked()
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""
    width: parent.width
    spacing: Style.space(8)

    Text {
      id: labelText
      textFormat: Text.PlainText
      text: parent.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Item { width: Math.max(0, parent.width - labelText.implicitWidth - valueText.implicitWidth - parent.spacing * 2); height: 1 }
    Text {
      id: valueText
      textFormat: Text.PlainText
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
