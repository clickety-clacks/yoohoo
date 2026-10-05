// Run only in an isolated Testbed Quickshell process. The staging directory
// provides qs.Commons/qs.Ui/qs.services links to the installed Omarchy sources
// and a payload/ copy of the production QML and selection module.
import QtQuick
import Quickshell
import "payload" as Yoohoo

ShellRoot {
  Yoohoo.Panel { id: indicator }
  Timer {
    interval: 200
    running: true
    onTriggered: {
      var now = Date.now()
      var row = {kind: "agent", id: "health-fixture", title: "Fixture", machine: "fixture"}
      indicator.nowMs = now
      indicator.applyPayload(JSON.stringify({windows: [row], hub: {
        enabled: true, status: "live", connected: true,
        lastSnapshotAtUnixMs: now - 3600000, lastSeenAtUnixMs: now
      }}))
      if (indicator.hubHealthStatus !== "live" || !indicator.hubConnected)
        throw new Error("quiet roster was not live with fresh heartbeat")
      indicator.nowMs = now + 46000
      if (indicator.hubHealthStatus !== "stale" || indicator.hubConnected)
        throw new Error("indicator failed to expire abandoned live status")
      if (indicator.windows.length !== 1 || indicator.rowCanActivate(row))
        throw new Error("stale roster was lost or allowed activation")
      indicator.applyPayload(JSON.stringify({windows: [], hub: {
        enabled: true, status: "reconnecting", connected: false,
        lastSnapshotAtUnixMs: now - 3600000, lastSeenAtUnixMs: now
      }}))
      if (indicator.windows.length !== 1 || indicator.hubHealthStatus !== "reconnecting")
        throw new Error("reconnect did not preserve last-known row")
      console.log("HUB_HEALTH_QML_PASS")
      Qt.quit()
    }
  }
}
