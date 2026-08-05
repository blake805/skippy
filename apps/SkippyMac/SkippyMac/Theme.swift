import SwiftUI

/// The one place visual decisions live. Every card, pill, and empty state in the
/// app draws from here, so a change to the look is a change to this file rather
/// than a hunt through views.
enum Theme {
    static let cornerRadius: CGFloat = 12
    static let cardPadding: CGFloat = 14

    static let cardBackground = Color.primary.opacity(0.045)
    static let cardStroke = Color.primary.opacity(0.08)
    static let railBackground = Color.primary.opacity(0.025)

    /// Each mode owns a hue so the cockpit reads at a glance: blue is code,
    /// orange is hardware, purple is talk.
    static func accent(for mode: AgentMode) -> Color {
        switch mode {
        case .coding: return .blue
        case .re: return .orange
        case .chat: return .purple
        }
    }
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
                .frame(maxWidth: 340)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }
}
