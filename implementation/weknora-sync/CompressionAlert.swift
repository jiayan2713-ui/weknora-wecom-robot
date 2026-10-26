import AppKit
import Foundation

final class AlertDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        guard CommandLine.arguments.count == 2 else { NSApp.terminate(nil); return }
        let path = URL(fileURLWithPath: CommandLine.arguments[1])
        do {
            let data = try Data(contentsOf: path)
            guard data.count < 1_000_000,
                  let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let title = payload["title"] as? String,
                  let body = payload["body"] as? String else { throw NSError(domain: "InvalidPayload", code: 1) }
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = title
            alert.informativeText = body + "\n\n此提醒由本机系统任务发出，不依赖 Codex。"
            alert.addButton(withTitle: "定位处理结果")
            alert.addButton(withTitle: "稍后查看")
            NSApp.activate(ignoringOtherApps: true)
            NSSound(named: "Basso")?.play()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                let receipt: [String: Any] = ["shown": true, "at": ISO8601DateFormatter().string(from: Date())]
                if let encoded = try? JSONSerialization.data(withJSONObject: receipt) {
                    try? encoded.write(to: path.deletingPathExtension().appendingPathExtension("shown.json"), options: .atomic)
                }
            }
            let response = alert.runModal()
            if response == .alertFirstButtonReturn {
                let report = payload["report_file"] as? String ?? ""
                let details = payload["details_file"] as? String ?? ""
                let selected = FileManager.default.fileExists(atPath: report) ? report : details
                if !selected.isEmpty {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: selected)])
                }
            }
            let acknowledged: [String: Any] = ["acknowledged": true, "at": ISO8601DateFormatter().string(from: Date())]
            let encoded = try JSONSerialization.data(withJSONObject: acknowledged)
            try encoded.write(to: path.deletingPathExtension().appendingPathExtension("ack.json"), options: .atomic)
        } catch {
            fputs("Compression alert failed: \(error)\n", stderr)
        }
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AlertDelegate()
app.delegate = delegate
app.run()
