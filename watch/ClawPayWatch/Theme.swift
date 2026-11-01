import SwiftUI

/// ClawPay palette: deep navy background, PayPal-ish blue to teal accent, warm amber for "act now".
enum Theme {
    static let accent = LinearGradient(colors: [Color(red: 0.0, green: 0.44, blue: 0.95),
                                                Color(red: 0.0, green: 0.78, blue: 0.80)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing)
    static let accentSolid = Color(red: 0.0, green: 0.60, blue: 0.92)
    static let approval = Color(red: 1.0, green: 0.70, blue: 0.20)
    static let paid = Color(red: 0.25, green: 0.85, blue: 0.50)
    static let background = LinearGradient(colors: [Color(red: 0.03, green: 0.06, blue: 0.16), .black],
                                           startPoint: .top, endPoint: .bottom)
    static let assistantBubble = Color.white.opacity(0.12)
}
