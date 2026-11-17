import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Bar entry point: the phone icon, the Tether service, and the shell IPC.
// Panel.qml is loaded here and receives the service, so the icon and the
// panel always read the same state. open/close/opened are forwarded so
// `omarchy-shell shell summon|hide io.github.sgatter.omatinuity` reach the panel.
BarWidget {
  id: root
  moduleName: "io.github.sgatter.omatinuity"

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.settings = root.settings
    target.anchorItem = button
    target.hostWidget = root
  }

  function glyph(cp) { return String.fromCodePoint(cp) }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Service {
    id: tether
    settings: root.settings
  }

  Loader {
    id: panelLoader
    active: true
    visible: false
    // Initial properties, so the panel's bindings never see a null service.
    Component.onCompleted: setSource(Qt.resolvedUrl("Panel.qml"), { tether: tether })
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  // Extras beyond summon/hide: keybindings, and bin/tether-gtk when a Tether
  // message notification is clicked.
  ShellIpc {
    target: root.moduleName

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { tether.refresh(); return "ok" }
    function status(): string {
      if (tether.ringing) return "ringing"
      if (tether.inCall) return "in-call"
      return tether.connected ? "connected" : (tether.active ? "waiting" : "stopped")
    }
    function send(path: string): string { return tether.sendFiles([path]) ? "sending" : "busy" }
    function sendClipboard(): string { return tether.sendClipboardText() ? "sending" : "busy" }
    function sendCopiedFiles(): string { return tether.sendCopiedFiles() ? "sending" : "busy" }
    function sendScreenshot(): string { return tether.sendScreenshot() ? "sending" : "busy" }
    function answer(): string { return tether.answer(tether.ringing ? tether.ringingCall.path : "") ? "ok" : "busy" }
    function hangup(): string { return tether.hangup("") ? "ok" : "busy" }
    function dial(number: string): string { return tether.dial(number) ? "dialing" : "busy" }
    function dialer(): void {
      root.open()
      if (panelLoader.item) panelLoader.item.openDialer()
    }
    function openThread(thread: string): string {
      if (!tether.validThread(thread) || !panelLoader.item) return "no thread"
      var info = tether.threadFor(thread)
      root.open()
      panelLoader.item.openConversation(thread, info ? info.name : "", info ? info.number : "")
      return "ok"
    }
    function openLatest(): string {
      if (tether.threads.length === 0 || !panelLoader.item) return "no conversations"
      root.open()
      panelLoader.item.openConversation(tether.threads[0].thread, tether.threads[0].name, tether.threads[0].number)
      return "ok"
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: tether.ringing ? root.glyph(0xF11AB)
      : (tether.inCall ? root.glyph(0xF03F2)
      : (tether.unread > 0 ? root.glyph(0xF0369) : root.glyph(0xF011C)))
    // Urgent colour for a ringing call or a pairing request; dim while no
    // phone is linked over Wi-Fi or Bluetooth.
    active: tether.hasPending || tether.ringing || tether.unread > 0
    useActiveColor: tether.hasPending || tether.ringing
    dimmed: !active && !tether.phoneHere && !tether.inCall
    tooltipText: tether.ringing ? "Incoming call: " + tether.callLabel(tether.ringingCall)
      : (tether.inCall ? "In call: " + tether.callLabel(tether.calls[0])
      : (tether.hasPending ? "iPhone wants to pair"
      : (tether.unread > 0 ? tether.unread + (tether.unread === 1 ? " unread message" : " unread messages")
      : (tether.phoneHere ? tether.phoneName + " connected"
      : (tether.active ? "Tether: waiting for the iPhone" : "Tether: daemon stopped")))))
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) tether.refresh()
      else if (buttonCode === Qt.MiddleButton) {
        if (tether.ringing) tether.answer(tether.ringingCall.path)
        else tether.sendClipboardText()
      }
      else root.toggle()
    }
  }
}
