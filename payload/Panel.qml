import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Selection.js" as Selection

Panel {
  id: root
  moduleName: "window-attention.indicator"
  ipcTarget: "window-attention.indicator"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color surface: Color.popups.background
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  property var windows: []
  property string selectedAddress: ""
  property bool cycling: false
  property bool accepting: false
  property var pendingSteps: []
  property var commandStreams: ({})
  property var streamOrder: []
  property string errorText: ""
  property double nowMs: Date.now()
  readonly property string attentionCommand: Quickshell.env("HOME") + "/.local/bin/window-attention"

  function refreshNow() {
    if (listProcess.running) return
    listProcess.command = [root.attentionCommand, "list"]
    listProcess.running = true
  }

  function applyPayload(text) {
    try {
      var payload = JSON.parse(String(text || ""))
      var previous = windows
      windows = payload.windows || []
      selectedAddress = Selection.reconcile(previous, windows, selectedAddress)
      if (pendingSteps.length) {
        selectedAddress = ""
        for (var i = 0; i < pendingSteps.length; ++i)
          selectedAddress = Selection.step(windows, selectedAddress, pendingSteps[i])
        pendingSteps = []
      }
      errorText = ""
      if (accepting) finishAccept()
    } catch (error) {
      errorText = "Attention state is unreadable"
      accepting = false
    }
  }

  function cycle(direction) {
    direction = direction < 0 ? -1 : 1
    if (!opened) {
      root.open()
      selectedAddress = ""
      pendingSteps = [direction]
    } else if (pendingSteps.length) {
      pendingSteps = pendingSteps.concat([direction])
    }
    cycling = true
    selectedAddress = Selection.step(windows, selectedAddress, direction)
  }

  // Optional ordered transport for keybinds that launch separate IPC processes.
  // A stream starts at sequence 1; callers choose its ID and key/modifier policy.
  function orderedCommand(action, streamId, sequence) {
    if (["next", "previous", "accept", "cancel"].indexOf(action) < 0 || sequence < 1) return
    if (!streamId.length || streamId.length > 128) return
    streamId = "stream:" + streamId
    if (!commandStreams[streamId]) {
      commandStreams[streamId] = { next: 1, pending: {} }
      streamOrder.push(streamId)
      if (streamOrder.length > 64) delete commandStreams[streamOrder.shift()]
    }
    var commands = Selection.enqueue(commandStreams[streamId], sequence, action)
    for (var i = 0; i < commands.length; ++i) {
      if (commands[i] === "next") cycle(1)
      else if (commands[i] === "previous") cycle(-1)
      else if (commands[i] === "accept") acceptCycle()
      else root.close()
    }
  }

  function moveSelection(direction) {
    if (pendingSteps.length) pendingSteps = pendingSteps.concat([direction])
    selectedAddress = Selection.step(windows, selectedAddress, direction)
  }

  function acceptCycle() {
    if (!opened || !cycling || accepting) return
    acceptSelection()
  }

  function acceptSelection() {
    if (!opened || accepting) return
    accepting = true
    refreshNow() // Validate against a fresh list before activating.
  }

  function finishAccept() {
    accepting = false
    var address = selectedAddress
    if (address) focusAddress(address)
    else root.close()
  }

  function age(timestamp) {
    var seconds = Math.max(0, Math.floor((nowMs - Number(timestamp) * 1000) / 1000))
    if (seconds < 60) return "now"
    var minutes = Math.floor(seconds / 60)
    if (minutes < 60) return minutes + "m"
    var hours = Math.floor(minutes / 60)
    if (hours < 24) return hours + "h " + (minutes % 60) + "m"
    return Math.floor(hours / 24) + "d " + (hours % 24) + "h"
  }

  function focusAddress(address) {
    if (focusProcess.running) return
    focusProcess.command = [root.attentionCommand, "focus", String(address)]
    focusProcess.running = true
    root.close()
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refreshNow()
  onOpenedChanged: {
    if (opened) {
      selectedAddress = windows.length ? windows[0].address : ""
      nowMs = Date.now()
      refreshNow()
    } else {
      cycling = false
      accepting = false
      pendingSteps = []
    }
  }

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: {
      root.nowMs = Date.now()
      root.refreshNow()
    }
  }

  Process {
    id: listProcess
    running: false
    onExited: function(exitCode, exitStatus) {
      if (exitCode === 0) root.applyPayload(listOutput.text)
      else {
        root.errorText = "Yoohoo attention state is unavailable"
        root.accepting = false
      }
    }
    stdout: StdioCollector {
      id: listOutput
      waitForEnd: true
    }
    stderr: StdioCollector {
      waitForEnd: true
    }
  }

  Process {
    id: focusProcess
    running: false
    onExited: root.refreshNow()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function next(): void { root.cycle(1) }
    function previous(): void { root.cycle(-1) }
    function accept(): void { root.acceptCycle() }
    function cancel(): void { root.close() }
    function ordered(action: string, stream: string, sequence: int): void {
      root.orderedCommand(action, stream, sequence)
    }
    function refresh(): string { root.refreshNow(); return "ok" }
    function status(): string {
      return JSON.stringify({ opened: root.opened, windows: root.windows,
                              selectedAddress: root.selectedAddress, cycling: root.cycling,
                              selectedIndex: attentionList.currentIndex, scrollY: attentionList.contentY,
                              error: root.errorText, hasBar: root.bar !== null })
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.windows.length > 0 ? "󰂚 " + root.windows.length : "󰂜"
    labelVisible: true
    tooltipText: root.windows.length > 0
      ? root.windows.length + " window" + (root.windows.length === 1 ? "" : "s") + " need attention"
      : "No windows need attention"
    active: root.windows.length > 0
    useActiveColor: false
    foreground: root.windows.length > 0 ? root.urgent : root.foreground
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (dy === 0 || root.windows.length === 0) return
        root.moveSelection(dy)
      }
      onActivateRequested: {
        root.acceptSelection()
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.moveSelection(direction) }
      onTextKey: function(text) { if (text === "r" || text === "R") root.refreshNow() }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "Yoohoo"
          meta: root.windows.length > 0
            ? root.windows.length + " waiting · Enter opens"
            : "Nothing is waiting"
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              text: "󰂚"
              color: root.windows.length > 0 ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        Text {
          visible: root.errorText !== ""
          width: parent.width
          text: root.errorText
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }

        Text {
          visible: root.errorText === "" && root.windows.length === 0
          width: parent.width
          text: "Applications can ask for attention without interrupting your current window. They will appear here."
          color: root.foreground
          opacity: 0.7
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }

        ListView {
          id: attentionList
          visible: root.windows.length > 0
          width: parent.width
          height: Math.min(contentHeight, Style.space(440))
          model: root.windows
          spacing: Style.space(6)
          clip: true
          currentIndex: Selection.indexOf(root.windows, root.selectedAddress)
          onCurrentIndexChanged: if (currentIndex >= 0)
            Qt.callLater(function() { attentionList.positionViewAtIndex(attentionList.currentIndex, ListView.Contain) })

          delegate: Rectangle {
            required property var modelData
            required property int index
            width: attentionList.width
            height: Style.space(68)
            radius: Style.cornerRadius
            color: index === attentionList.currentIndex
              ? Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.14)
              : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.05)
            border.width: index === attentionList.currentIndex ? 1 : 0
            border.color: root.urgent

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              onEntered: if (!root.cycling) root.selectedAddress = modelData.address
              onClicked: root.focusAddress(modelData.address)
            }

            Column {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.margins: Style.space(12)
              spacing: Style.space(4)

              Text {
                width: parent.width
                text: modelData.title || modelData.class || "Untitled window"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: (modelData.class || "Application")
                  + " · workspace " + (modelData.workspace || "?")
                  + " · " + root.age(modelData.first_attention_at)
                  + (Number(modelData.count || 1) > 1 ? " · ×" + modelData.count : "")
                color: root.foreground
                opacity: 0.65
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }
        }

        Text {
          width: parent.width
          text: "Tab/Shift+Tab or ↑/↓ select · Enter open · Esc close"
          color: root.foreground
          opacity: 0.55
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignHCenter
        }
      }
    }
  }
}
