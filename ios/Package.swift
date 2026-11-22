// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VoiceNotifierClientTests",
    platforms: [.iOS(.v17), .macOS(.v13)],
    targets: [
        .target(name: "PlaybackQueue", path: "VoiceNotifier", exclude: ["VoiceNotifierApp.swift"]),
        .testTarget(name: "PlaybackQueueTests", dependencies: ["PlaybackQueue"], path: "PlaybackQueueTests"),
    ]
)
