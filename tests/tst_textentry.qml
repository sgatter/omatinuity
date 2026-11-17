import QtQuick
import QtTest
import ".."

// TextEntry never lets Qt read the clipboard: run by tests/test_textentry.py
// through qmltestrunner on the offscreen platform.
Item {
  id: scene
  width: 400
  height: 300

  // Fills the clipboard without any clipboard API of our own.
  TextEdit { id: source; text: "CLIP" }

  // Control: an ordinary writable TextEdit does paste natively.
  TextEdit { id: plain; y: 40; width: 300; height: 30 }

  TextEntry {
    id: entry
    y: 100
    width: 300
    maxLength: 12
  }

  TextEntry {
    id: wide
    y: 160
    width: 300
    maxLines: 3
    maxLength: 400
  }

  SignalSpy { id: pasteSpy; target: entry; signalName: "pasteRequested" }
  SignalSpy { id: acceptSpy; target: entry; signalName: "accepted" }
  SignalSpy { id: cancelSpy; target: entry; signalName: "cancelled" }
  SignalSpy { id: downSpy; target: entry; signalName: "downRequested" }

  function typesIn(item, found) {
    found.push(String(item))
    for (var i = 0; i < item.children.length; i++) typesIn(item.children[i], found)
    return found
  }

  TestCase {
    name: "TextEntry"
    when: windowShown

    function fillClipboard() {
      source.selectAll()
      source.copy()
    }

    function init() {
      entry.text = ""
      wide.text = ""
      plain.text = ""
      pasteSpy.clear(); acceptSpy.clear(); cancelSpy.clear(); downSpy.clear()
      entry.forceActiveFocus()
      verify(entry.activeFocus)
    }

    function test_no_text_input_inside() {
      var types = scene.typesIn(entry, [])
      for (var i = 0; i < types.length; i++)
        verify(types[i].indexOf("QQuickTextInput") === -1 && types[i].indexOf("QQuickTextField") === -1, types[i])
      verify(types.join(" ").indexOf("QQuickTextEdit") !== -1)
    }

    function test_typing() {
      keyClick(Qt.Key_H); keyClick(Qt.Key_I)
      compare(entry.text, "hi")
      keyClick(Qt.Key_Backspace)
      compare(entry.text, "h")
    }

    function test_clipboard_reaches_an_ordinary_textedit() {
      fillClipboard()
      plain.forceActiveFocus()
      keySequence(StandardKey.Paste)
      compare(plain.text, "CLIP")
    }

    function test_paste_keys_never_paste_natively() {
      fillClipboard()
      keyClick(Qt.Key_A)
      keySequence(StandardKey.Paste)
      keyClick(Qt.Key_V, Qt.ControlModifier)
      keyClick(Qt.Key_V, Qt.ControlModifier | Qt.ShiftModifier)
      keyClick(Qt.Key_Insert, Qt.ShiftModifier)
      compare(entry.text, "a")
      compare(pasteSpy.count, 4)
      for (var i = 0; i < pasteSpy.count; i++) compare(pasteSpy.signalArguments[i][0], false)
    }

    function test_middle_click_asks_instead_of_pasting() {
      fillClipboard()
      mouseClick(entry, 20, entry.height / 2, Qt.MiddleButton)
      compare(entry.text, "")
      compare(pasteSpy.count, 1)
      compare(pasteSpy.signalArguments[0][0], true)
    }

    function test_left_click_still_focuses() {
      plain.forceActiveFocus()
      verify(!entry.activeFocus)
      mouseClick(entry, 20, entry.height / 2, Qt.LeftButton)
      verify(entry.activeFocus)
    }

    function test_enter_escape_down_tab() {
      keyClick(Qt.Key_A)
      keyClick(Qt.Key_Return); keyClick(Qt.Key_Enter)
      keyClick(Qt.Key_Return, Qt.ShiftModifier)
      keyClick(Qt.Key_Escape)
      keyClick(Qt.Key_Down)
      keyClick(Qt.Key_Tab); keyClick(Qt.Key_Backtab)
      compare(entry.text, "a")
      compare(acceptSpy.count, 3)
      compare(cancelSpy.count, 1)
      compare(downSpy.count, 1)
      verify(entry.activeFocus)
    }

    function test_insert_text_is_one_bounded_paragraph() {
      entry.insertText("one\ntwo\r\n\tthree and far too long")
      compare(entry.text, "one two thre")
      compare(entry.length, 12)
      entry.insertText("more")
      compare(entry.text, "one two thre")
    }

    function test_insert_text_replaces_the_selection_at_the_cursor() {
      wide.forceActiveFocus()
      wide.text = "hello world"
      var edit = findChild(wide, "edit")
      edit.select(0, 5)
      wide.insertText("bye")
      compare(wide.text, "bye world")
      compare(wide.cursorPosition, 3)
    }

    function test_never_ends_inside_a_surrogate_pair() {
      entry.text = "12345678901"
      entry.insertText("😀")
      compare(entry.text, "12345678901")
      compare(entry.fitted("ab😀", 3), "ab")
      compare(entry.fitted("ab😀", 4), "ab😀")
    }

    function test_typing_and_assignment_respect_the_limits() {
      for (var i = 0; i < 20; i++) keyClick(Qt.Key_X)
      compare(entry.length, 12)
      entry.text = "a\nb"
      compare(entry.text, "a b")
    }

    function test_grows_to_max_lines_then_stops() {
      var one = wide.implicitHeight
      wide.text = "word ".repeat(70)
      verify(wide.implicitHeight > one * 2.5)
      verify(wide.implicitHeight <= one * 3 + 2)
      entry.text = "x".repeat(12)
      compare(entry.implicitHeight, one)
    }
  }
}
