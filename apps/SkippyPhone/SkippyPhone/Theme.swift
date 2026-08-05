import SwiftUI
import UIKit

/// The one place visual decisions live — the same design system as the Mac
/// cockpit, adapted for a phone. Every card, pill, and empty state draws from
/// here, so the two apps read as one product.
enum Theme {
    static let cornerRadius: CGFloat = 12
    static let cardPadding: CGFloat = 14

    static let cardBackground = Color.primary.opacity(0.045)
    static let cardStroke = Color.primary.opacity(0.08)

    /// Each mode owns a hue: blue is code, orange is hardware, purple is talk.
    static func accent(for mode: AgentMode) -> Color {
        switch mode {
        case .coding: return .blue
        case .re: return .orange
        case .chat: return .purple
        }
    }

    /// The voice lane's own hue, used by the orb and waveform.
    static let voice = Color.purple
}

/// The standard card treatment: soft fill, hairline stroke, rounded.
struct CardStyle: ViewModifier {
    var accent: Color = .clear

    func body(content: Content) -> some View {
        content
            .padding(Theme.cardPadding)
            .background(Theme.cardBackground)
            .clipShape(RoundedRectangle(cornerRadius: Theme.cornerRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.cornerRadius)
                    .strokeBorder(accent == .clear ? Theme.cardStroke : accent.opacity(0.35), lineWidth: 1)
            )
    }
}

extension View {
    func card(accent: Color = .clear) -> some View {
        modifier(CardStyle(accent: accent))
    }
}

/// A compact colored capsule for run and connection states.
struct StatusPill: View {
    let text: String
    let color: Color
    var pulsing: Bool = false

    @State private var dim = false

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(dim ? 0.10 : 0.18))
            .clipShape(Capsule())
            .onAppear {
                guard pulsing else { return }
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    dim = true
                }
            }
    }
}

/// What an empty pane says instead of showing nothing.
struct EmptyState: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 34, weight: .light))
                .foregroundStyle(.tertiary)
            Text(title)
                .font(.headline)
                .foregroundStyle(.secondary)
            Text(message)
                .font(.callout)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 320)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(32)
    }
}

/// Haptics, named for the moment rather than the UIKit class, so call sites
/// read as intent. Generators are kept warm — allocating one per tap adds
/// latency to exactly the feedback that must feel instant.
enum Haptics {
    private static let light = UIImpactFeedbackGenerator(style: .light)
    private static let medium = UIImpactFeedbackGenerator(style: .medium)
    private static let notice = UINotificationFeedbackGenerator()

    /// Push-to-talk pressed, or a small state change.
    static func tap() {
        light.impactOccurred()
    }

    /// Skippy started or stopped speaking; talk released.
    static func shift() {
        medium.impactOccurred()
    }

    /// An approval card arrived — the one moment the phone must get the
    /// user's attention even in a pocket.
    static func attention() {
        notice.notificationOccurred(.warning)
    }

    static func success() {
        notice.notificationOccurred(.success)
    }
}
