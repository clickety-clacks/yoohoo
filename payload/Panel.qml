import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import qs.Ui as OmarchyUi
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
  // This remains named selectedAddress for IPC compatibility, but contains the
  // stable row key (id for hub rows, address for native compositor rows).
  property string selectedAddress: ""
  property bool cycling: false
  property bool accepting: false
  property var pendingSteps: []
  property var commandStreams: ({})
  property var streamOrder: []
  property string errorText: ""
  property string actionErrorText: ""
  property var hub: ({ enabled: false, configured: false, status: "disabled",
                       connected: false, error: "" })
  property var pendingActivation: null
  // The entry whose activation failed, kept until the user dismisses or
  // keeps it. A stuck entry must always have a user-operable way out.
  property var failedRow: null
  property string failureText: ""
  property double nowMs: Date.now()
  readonly property string attentionCommand: Quickshell.env("HOME") + "/.local/bin/window-attention"
  readonly property string windowIcon: "▣"
  readonly property string connectIcon: "↗"
  readonly property bool hubConfigured: root.hub && root.hub.configured === true
  readonly property string hubHealthStatus: Selection.healthStatus(root.hub, root.nowMs)
  readonly property bool hubConnected: !root.hubConfigured
    || Selection.hubIsLive(root.hub, root.nowMs)
  readonly property bool hubNeedsAttention: root.hubConfigured
    && Selection.hubNeedsAttention(root.hub, root.nowMs)
  // A fresh list/connection failure is more important than an older action
  // failure; keep the latter visible when the current state is healthy.
  readonly property string displayErrorText: root.errorText || root.actionErrorText

  function hasId(row) {
    return Selection.hasId(row)
  }

  function rowKey(row) {
    return Selection.key(row)
  }

  function isAgent(row) {
    return Selection.isAgent(row)
  }

  function rowUnavailable(row) {
    return Selection.unavailable(row, root.hubConfigured, root.hubConnected)
  }

  function rowUnavailableReason(row) {
    if (root.hubConfigured && !root.hubConnected)
      return Selection.unavailableReason(root.hub, root.nowMs)
    var code = String(row && row.unavailable_reason || "")
    return Selection.plainReason(code, row) || code || "Connection unavailable"
  }

  function rowCanActivate(row) {
    return Selection.canActivate(row, root.hubConfigured, root.hubConnected)
  }

  // Keep activation feedback attached to the stable row identity even when a
  // fresh list replaces the row objects while the command is in flight.
  function isPendingActivation(row) {
    var pendingKey = root.rowKey(root.pendingActivation)
    return root.pendingActivation !== null && pendingKey !== ""
      && root.rowKey(row) === pendingKey
  }

  function activationStatus(row) {
    if (!root.isPendingActivation(row)) return ""
    return root.isAgent(root.pendingActivation) && root.pendingActivation.open_on_machine !== true
      ? "Connecting…" : "Opening…"
  }

  function actionFailureText(row, raw) {
    return Selection.failureReason(row, raw)
  }

  function showFailure(row, raw) {
    if (!row) return
    actionErrorText = ""
    failureText = actionFailureText(row, raw)
    failedRow = row
  }

  function clearFailure() {
    failedRow = null
    failureText = ""
  }

  function dismissFailed() {
    var row = failedRow
    if (!row || dismissProcess.running) return
    var key = rowKey(row)
    if (!key) { clearFailure(); return }
    dismissProcess.command = [root.attentionCommand, "dismiss", String(key)]
    dismissProcess.running = true
  }

  function rowTitle(row) {
    if (!row) return "Untitled window"
    if (root.isAgent(row)) return row.title || row.name || row.machine || "Untitled agent"
    return row.title || row.class || "Untitled window"
  }

  function rowIcon(row) {
    return Selection.iconKind(row) === "connect" ? root.connectIcon : root.windowIcon
  }

  function rowIconDescription(row) {
    if (root.isAgent(row) && row.open_on_machine !== true)
      return "Connect icon"
    return "Window icon"
  }

  function rowAccessibleDescription(row) {
    var title = root.rowTitle(row)
    var machine = row && row.machine ? " on " + row.machine : ""
    if (root.rowUnavailable(row))
      return title + machine + ". Unavailable: " + root.rowUnavailableReason(row)
    if (root.isAgent(row) && row.open_on_machine !== true)
      return title + machine + ". Connect to agent"
    return title + machine + ". Open window"
  }

  function selectedRow() {
    var index = Selection.indexOf(root.windows, root.selectedAddress)
    return index >= 0 ? root.windows[index] : null
  }

  function normalizedHub(value) {
    var candidate = value || {}
    var enabled = candidate.enabled === true
    var configured = candidate.configured === true || enabled
    var status = Selection.validHealthStatus(candidate.status)
      ? candidate.status
      : (enabled ? "stale" : "disabled")
    return {
      enabled: enabled,
      configured: configured,
      status: status,
      connected: candidate.connected === true,
      lastSnapshotAtUnixMs: candidate.lastSnapshotAtUnixMs,
      lastSeenAtUnixMs: candidate.lastSeenAtUnixMs,
      error: String(candidate.error || "")
    }
  }

  function hubWarningText() {
    return Selection.hubWarning(root.hub, root.windows, root.nowMs)
  }

  function hubTooltipText() {
    var waiting = root.windows.length > 0
      ? root.windows.length + " window" + (root.windows.length === 1 ? "" : "s") + " need attention"
      : "No windows need attention"
    if (!root.hubConfigured) return waiting + " · Hub disabled"
    return waiting + " · " + Selection.hubStatusSummary(root.hub, root.nowMs)
  }

  function staleHubState(base, reason) {
    var source = base || root.hub || {}
    return {
      enabled: true,
      configured: true,
      status: "stale",
      connected: false,
      lastSnapshotAtUnixMs: source.lastSnapshotAtUnixMs,
      lastSeenAtUnixMs: source.lastSeenAtUnixMs,
      error: String(reason || "Hub state is unavailable")
    }
  }

  function markHubStale(reason) {
    if (!root.hubConfigured) return
    root.hub = staleHubState(root.hub, reason)
  }

  function refreshNow() {
    if (listProcess.running) return
    listProcess.command = [root.attentionCommand, "list"]
    listProcess.running = true
  }

  function applyPayload(text) {
    try {
      root.nowMs = Date.now()
      var payload = JSON.parse(String(text || ""))
      var previous = windows
      var nextHub = normalizedHub(payload.hub)
      var hasList = Array.isArray(payload.windows)
      var hasHubState = payload.hub !== null && typeof payload.hub === "object"
        && !Array.isArray(payload.hub)
      if (!hasList) {
        errorText = "Attention state is incomplete"
        if (root.hubConfigured)
          nextHub = {
            enabled: true,
            configured: true,
            status: "stale",
            connected: false,
            lastSnapshotAtUnixMs: root.hub.lastSnapshotAtUnixMs,
            lastSeenAtUnixMs: root.hub.lastSeenAtUnixMs,
            error: "Hub health is unavailable"
          }
      } else if (root.hubConfigured && !hasHubState) {
        nextHub = {
          enabled: true,
          configured: true,
          status: "stale",
          connected: false,
          lastSnapshotAtUnixMs: root.hub.lastSnapshotAtUnixMs,
          lastSeenAtUnixMs: root.hub.lastSeenAtUnixMs,
          error: "Hub health is unavailable"
        }
      }
      // A malformed/missing list must not erase the last-known roster. An
      // explicit array (including []) is a valid complete list response, but
      // an unavailable Hub response must retain remote rows as last-known.
      if (hasList) {
        windows = Selection.retainLastKnownRows(
          previous, payload.windows,
          Selection.healthStatus(nextHub, root.nowMs)
        )
      }
      var hasAgentRows = Selection.agentCount(windows) > 0
      var malformedHealth = !hasHubState
        || !Selection.validHealthStatus(payload.hub && payload.hub.status)
        || (payload.hub.status !== "disabled" && payload.hub.enabled !== true)
      if (hasAgentRows && (!hasList || malformedHealth || nextHub.status === "disabled"))
        nextHub = staleHubState(nextHub, "Hub health is unavailable")
      hub = nextHub
      selectedAddress = Selection.reconcile(previous, windows, selectedAddress)
      if (pendingSteps.length) {
        selectedAddress = ""
        for (var i = 0; i < pendingSteps.length; ++i)
          selectedAddress = Selection.step(windows, selectedAddress, pendingSteps[i])
        pendingSteps = []
      }
      if (hasList) {
        errorText = ""
        if (accepting) finishAccept()
      } else {
        accepting = false
      }
    } catch (error) {
      errorText = "Attention state is unreadable"
      markHubStale("Hub state is unreadable")
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
    var row = selectedRow()
    if (row && rowCanActivate(row)) activateRow(row)
    else if (row) showFailure(row, "")
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

  function activateRow(row) {
    if (focusProcess.running) return
    if (!rowCanActivate(row)) {
      showFailure(row, "")
      return
    }
    actionErrorText = ""
    clearFailure()
    pendingActivation = row
    focusProcess.command = Selection.activation(row) === "open"
      ? [root.attentionCommand, "open", String(row.id)]
      : [root.attentionCommand, "focus", String(row.address)]
    focusProcess.running = true
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refreshNow()
  onOpenedChanged: {
    if (opened) {
      actionErrorText = ""
      clearFailure()
      selectedAddress = windows.length ? Selection.key(windows[0]) : ""
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
        root.markHubStale("Hub state is unavailable")
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
    onExited: function(exitCode, exitStatus) {
      var row = root.pendingActivation
      root.pendingActivation = null
      if (exitCode === 0) {
        root.close()
        root.refreshNow()
      } else {
        root.showFailure(row, focusErrorOutput.text)
        root.accepting = false
        root.refreshNow()
      }
    }
    stderr: StdioCollector {
      id: focusErrorOutput
      waitForEnd: true
    }
  }

  Process {
    id: dismissProcess
    running: false
    onExited: function(exitCode, exitStatus) {
      if (exitCode === 0) {
        root.clearFailure()
        root.refreshNow()
      } else {
        root.actionErrorText = "Could not dismiss this entry. "
          + Selection.failureReason(root.failedRow, dismissErrorOutput.text)
      }
    }
    stderr: StdioCollector {
      id: dismissErrorOutput
      waitForEnd: true
    }
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
                              error: root.displayErrorText, hub: root.hub,
                              failure: root.failedRow === null ? null
                                : { key: root.rowKey(root.failedRow), text: root.failureText },
                              hubHealth: root.hubHealthStatus,
                              busy: root.pendingActivation !== null,
                              hasBar: root.bar !== null })
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: (root.windows.length > 0 ? "󰂚 " + root.windows.length : "󰂜")
      + (root.hubConfigured ? Selection.barHealthMarker(root.hub, root.nowMs) : "")
    labelVisible: true
    tooltipText: root.hubTooltipText()
    active: root.windows.length > 0
    useActiveColor: false
    foreground: root.windows.length > 0 || root.hubNeedsAttention
      ? root.urgent : root.foreground
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
      onCloseRequested: {
        if (root.failedRow !== null) root.clearFailure()
        else root.close()
      }
      onTabRequested: function(direction) { root.moveSelection(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.refreshNow()
        else if ((text === "d" || text === "D") && root.failedRow !== null) root.dismissFailed()
      }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "Yoohoo"
          meta: root.windows.length > 0
            ? root.windows.length + " waiting · "
              + (root.hubConfigured ? Selection.hubStatusSummary(root.hub, root.nowMs) + " · " : "")
              + "Enter opens"
            : (root.hubConfigured
              ? Selection.hubStatusSummary(root.hub, root.nowMs) + " · Nothing is waiting"
              : "Nothing is waiting · Hub disabled")
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
          visible: root.hubWarningText() !== ""
          width: parent.width
          text: root.hubWarningText()
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
          Accessible.role: Accessible.AlertMessage
          Accessible.name: text
        }

        Text {
          visible: root.displayErrorText !== ""
          width: parent.width
          text: root.displayErrorText
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
          Accessible.role: Accessible.AlertMessage
          Accessible.name: text
        }

        Rectangle {
          id: failureCard
          visible: root.failedRow !== null
          width: parent.width
          height: failureColumn.implicitHeight + Style.space(24)
          radius: Style.cornerRadius
          color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.10)
          border.width: 1
          border.color: root.urgent
          Accessible.role: Accessible.AlertMessage
          Accessible.name: "Could not open " + root.rowTitle(root.failedRow) + ". " + root.failureText

          Column {
            id: failureColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.space(12)
            spacing: Style.space(8)

            Text {
              width: parent.width
              text: "Could not open " + root.rowTitle(root.failedRow)
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              text: root.failureText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              text: "Dismissing clears this entry only. The agent keeps running and can ask again."
              color: root.foreground
              opacity: 0.65
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Row {
              spacing: Style.space(8)

              OmarchyUi.Button {
                text: dismissProcess.running ? "Dismissing…" : "Dismiss alert"
                bordered: true
                enabled: !dismissProcess.running
                foreground: root.urgent
                accent: root.urgent
                fontFamily: root.fontFamily
                tooltipText: "D"
                onClicked: root.dismissFailed()
              }

              OmarchyUi.Button {
                text: "Keep"
                bordered: true
                fontFamily: root.fontFamily
                tooltipText: "Esc"
                onClicked: root.clearFailure()
              }
            }
          }
        }

        Text {
          visible: root.hubWarningText() === "" && root.displayErrorText === "" && root.windows.length === 0
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
            readonly property bool activationPending: root.isPendingActivation(modelData)
            width: attentionList.width
            height: Style.space(68)
            radius: Style.cornerRadius
            color: index === attentionList.currentIndex
              ? Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.14)
              : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.05)
            border.width: index === attentionList.currentIndex ? 1 : 0
            border.color: root.urgent
            opacity: root.rowUnavailable(modelData) ? 0.58 : 1
            Accessible.role: Accessible.ListItem
            Accessible.name: root.rowTitle(modelData)
            Accessible.description: activationPending
              ? root.activationStatus(modelData)
              : root.rowAccessibleDescription(modelData)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              enabled: root.rowCanActivate(modelData)
              onEntered: if (!root.cycling) root.selectedAddress = root.rowKey(modelData)
              onClicked: root.activateRow(modelData)
            }

            Text {
              id: rowIcon
              anchors.left: parent.left
              anchors.leftMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(24)
              text: activationPending ? "󰦖" : root.rowIcon(modelData)
              color: root.rowUnavailable(modelData) ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
              transformOrigin: Item.Center
              rotation: 0
              RotationAnimator on rotation {
                from: 0
                to: 360
                duration: 800
                loops: Animation.Infinite
                running: activationPending && root.opened
                onStopped: rowIcon.rotation = 0
              }
              Accessible.role: Accessible.Graphic
              Accessible.name: activationPending ? "Busy" : root.rowIconDescription(modelData)
              Accessible.description: activationPending
                ? root.activationStatus(modelData)
                : root.rowAccessibleDescription(modelData)
            }

            Column {
              anchors.left: rowIcon.right
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.rightMargin: Style.space(12)
              anchors.leftMargin: Style.space(8)
              spacing: Style.space(4)

              Text {
                width: parent.width
                text: root.rowTitle(modelData)
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: {
                  var status = root.activationStatus(modelData)
                  if (status) return status
                  var parts = []
                  if (modelData.machine) parts.push(String(modelData.machine))
                  if (modelData.kind === "agent") {
                    parts.push(modelData.open_on_machine === true ? "window" : "connect")
                    if (root.rowUnavailable(modelData)) parts.push(root.rowUnavailableReason(modelData))
                  } else {
                    parts.push(modelData.class || "Application")
                    parts.push("workspace " + (modelData.workspace || "?"))
                    parts.push(root.age(modelData.first_attention_at))
                    if (Number(modelData.count || 1) > 1) parts.push("×" + modelData.count)
                  }
                  return parts.join(" · ")
                }
                color: root.rowUnavailable(modelData) ? root.urgent : root.foreground
                opacity: root.rowUnavailable(modelData) ? 0.9 : 0.65
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }
        }

        Text {
          width: parent.width
          text: root.failedRow !== null
            ? "D dismiss · Enter retry · Esc keep"
            : "Tab/Shift+Tab or ↑/↓ select · Enter open · Esc close"
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
