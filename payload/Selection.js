// Pure selection policy shared by the QML menu and regression tests.
function enqueue(stream, sequence, action) {
    if (sequence < stream.next || sequence > stream.next + 256) return [];
    stream.pending[sequence] = action;
    var ready = [];
    while (Object.prototype.hasOwnProperty.call(stream.pending, stream.next)) {
        ready.push(stream.pending[stream.next]);
        delete stream.pending[stream.next++];
    }
    return ready;
}
function indexOf(windows, address) {
    return windows.findIndex(function(w) { return w.address === address; });
}
function step(windows, address, direction) {
    if (!windows.length) return "";
    var index = indexOf(windows, address);
    if (index < 0) return windows[direction < 0 ? windows.length - 1 : 0].address;
    return windows[(index + direction + windows.length) % windows.length].address;
}
function reconcile(previous, windows, address) {
    if (indexOf(windows, address) >= 0) return address;
    var oldIndex = indexOf(previous, address);
    if (oldIndex >= 0) {
        for (var offset = 1; offset < previous.length; ++offset) {
            var candidate = previous[(oldIndex + offset) % previous.length].address;
            if (indexOf(windows, candidate) >= 0) return candidate;
        }
    }
    return windows.length ? windows[0].address : "";
}
