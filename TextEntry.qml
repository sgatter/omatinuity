import QtQuick

// The reply and dialer boxes. Built on TextEdit, not TextField/TextInput, and
// it never lets Qt read the clipboard:
//
//  - A writable TextInput asks the clipboard for its whole text every time the
//    clipboard changes, just to keep canPaste current (Qt before the fix for
//    QTBUG-149610). That read runs on the shell's GUI thread with no size
//    limit, so a hostile clipboard owner could stall the shell while one of
//    these boxes exists. TextEdit only looks at which formats are offered.
//  - Paste keys and middle-click are taken here before TextEdit sees them and
//    raise pasteRequested(). The service then fetches the text in the helper
//    process, bounded in size and time, and hands it to insertText().
//
// Only QtQuick is imported so tests/tst_textentry.qml can load this file
// without the shell; Panel.qml adds the shell's look.
FocusScope {
  id: root

  property alias text: edit.text
  property alias font: edit.font
  property alias color: edit.color
  property alias selectionColor: edit.selectionColor
  property alias selectedTextColor: edit.selectedTextColor
  readonly property alias length: edit.length
  readonly property alias cursorPosition: edit.cursorPosition
  readonly property alias selectedText: edit.selectedText
  property string placeholderText: ""
  property color placeholderTextColor: Qt.darker(edit.color, 1.6)
  // 1 keeps the text on one line and scrolls sideways. More lets the box wrap
  // and grow to that many lines, then scroll.
  property int maxLines: 1
  property int maxLength: 256
  property real leftPadding: 0
  property real rightPadding: 0
  property real topPadding: 0
  property real bottomPadding: 0
  property Item background: null
  readonly property bool hovered: hover.hovered

  signal accepted()
  signal cancelled()
  signal downRequested()
  // primary: the middle-click selection rather than the clipboard.
  signal pasteRequested(bool primary)

  property real _scrollX: 0
  property real _scrollY: 0
  property bool _enforcing: false

  // Line breaks of every kind, and tabs. The three beyond ASCII (next line,
  // line separator, paragraph separator) are added by code point so that this
  // file holds no invisible characters.
  readonly property var _breaks: new RegExp("[\\r\\n\\t\\x0b\\x0c" + String.fromCharCode(0x85, 0x2028, 0x2029) + "]+", "g")

  // One paragraph, no tabs: these are single messages and search terms.
  function singleLine(value) {
    return String(value === undefined || value === null ? "" : value).replace(_breaks, " ")
  }

  // The first `count` UTF-16 units of `value`, never ending inside a pair.
  function fitted(value, count) {
    if (value.length <= count) return value
    var cut = Math.max(0, count)
    var last = cut > 0 ? value.charCodeAt(cut - 1) : 0
    if (last >= 0xD800 && last <= 0xDBFF) cut -= 1
    return value.substring(0, cut)
  }

  // Text from outside the keyboard (a paste): replaces the selection, lands
  // at the cursor, and is cut to what still fits.
  function insertText(value) {
    var t = singleLine(value)
    if (edit.selectionStart !== edit.selectionEnd) edit.remove(edit.selectionStart, edit.selectionEnd)
    t = fitted(t, maxLength - edit.length)
    if (t !== "") edit.insert(edit.cursorPosition, t)
  }

  // Whatever put it there (typing, an input method, an assignment), the text
  // stays one paragraph within maxLength.
  function _enforce() {
    if (_enforcing) return
    var now = edit.text
    var want = fitted(singleLine(now), maxLength)
    if (want === now) return
    _enforcing = true
    var at = edit.cursorPosition
    edit.text = want
    edit.cursorPosition = Math.min(at, edit.length)
    _enforcing = false
  }

  function _follow() {
    var c = edit.cursorRectangle
    var x = _scrollX
    var y = _scrollY
    if (c.x < x) x = c.x
    else if (c.x + c.width > x + viewport.width) x = c.x + c.width - viewport.width
    if (c.y < y) y = c.y
    else if (c.y + c.height > y + viewport.height) y = c.y + c.height - viewport.height
    _scrollX = Math.max(0, Math.min(x, Math.max(0, edit.width - viewport.width)))
    _scrollY = Math.max(0, Math.min(y, Math.max(0, edit.height - viewport.height)))
  }

  implicitWidth: 200
  implicitHeight: viewport.height + topPadding + bottomPadding

  onBackgroundChanged: {
    if (!background) return
    background.parent = root
    background.z = -1
    background.anchors.fill = root
  }

  FontMetrics {
    id: metrics
    font: edit.font
  }

  HoverHandler { id: hover }

  // A click on the padding focuses the box too.
  TapHandler { onTapped: edit.forceActiveFocus() }

  Item {
    id: viewport
    x: root.leftPadding
    y: root.topPadding
    width: Math.max(1, root.width - root.leftPadding - root.rightPadding)
    height: Math.ceil(Math.max(metrics.lineSpacing, Math.min(edit.implicitHeight, Math.max(1, root.maxLines) * metrics.lineSpacing)))
    clip: true
    onWidthChanged: root._follow()
    onHeightChanged: root._follow()

    TextEdit {
      id: edit
      objectName: "edit"
      x: -root._scrollX
      y: -root._scrollY
      width: root.maxLines <= 1 ? Math.max(viewport.width, implicitWidth) : viewport.width
      focus: true
      textFormat: TextEdit.PlainText
      wrapMode: root.maxLines <= 1 ? TextEdit.NoWrap : TextEdit.Wrap
      selectByMouse: true
      persistentSelection: false

      onTextChanged: root._enforce()
      onCursorRectangleChanged: root._follow()
      onWidthChanged: root._follow()
      onHeightChanged: root._follow()

      // Before TextEdit's own handling (Keys.BeforeItem is the default), so
      // none of these reach it: no native paste, no newline, no tab.
      Keys.onPressed: function(event) {
        var mods = event.modifiers
        var ctrl = (mods & Qt.ControlModifier) !== 0
        if (event.matches(StandardKey.Paste) || event.key === Qt.Key_Paste
            || (ctrl && event.key === Qt.Key_V)
            || (event.key === Qt.Key_Insert && (mods & Qt.ShiftModifier))) {
          root.pasteRequested(false)
          event.accepted = true
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
          root.accepted()
          event.accepted = true
        } else if (event.key === Qt.Key_Escape) {
          root.cancelled()
          event.accepted = true
        } else if (event.key === Qt.Key_Down && root.maxLines <= 1) {
          root.downRequested()
          event.accepted = true
        } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab || event.key === Qt.Key_Menu) {
          event.accepted = true
        }
      }
    }

    Text {
      objectName: "placeholder"
      visible: edit.length === 0 && edit.preeditText === ""
      width: viewport.width
      textFormat: Text.PlainText
      text: root.placeholderText
      color: root.placeholderTextColor
      font: edit.font
      elide: Text.ElideRight
    }

    // TextEdit pastes the primary selection on a middle-button release. This
    // takes that button first; every other button passes through to the text.
    MouseArea {
      objectName: "middleClick"
      anchors.fill: parent
      acceptedButtons: Qt.MiddleButton
      cursorShape: Qt.IBeamCursor
      onClicked: {
        edit.forceActiveFocus()
        root.pasteRequested(true)
      }
    }
  }
}
