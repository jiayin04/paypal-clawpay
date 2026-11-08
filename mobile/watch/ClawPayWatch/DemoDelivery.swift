import Foundation
#if os(watchOS)
import CryptoKit
#endif

/// Payment/delivery is still a demo; user-selected addresses never reach the shopping agent.
struct DemoDelivery: Codable, Equatable {
    enum Stage: String, Codable, CaseIterable {
        case awaitingPayment, confirmed, preparing, shipped, delivered
        var label: String {
            switch self {
            case .awaitingPayment: "Awaiting payment"
            case .confirmed: "Order confirmed"
            case .preparing: "Preparing package"
            case .shipped: "Shipped"
            case .delivered: "Delivered"
            }
        }
    }
    struct Address: Identifiable, Codable, Equatable {
        enum Source: String, Codable { case demo, manual, currentLocation, saved }
        var id = UUID().uuidString
        var label: String
        var lines: String
        var unit = ""
        var source = Source.demo

        var normalized: Self {
            var copy = self
            copy.label = label.trimmingCharacters(in: .whitespacesAndNewlines)
            copy.lines = lines.components(separatedBy: .newlines)
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }.joined(separator: "\n")
            copy.unit = unit.trimmingCharacters(in: .whitespacesAndNewlines)
            return copy
        }
        var isValid: Bool {
            let value = normalized
            let text = value.label + value.lines + value.unit
            return !value.id.isEmpty && (1...40).contains(value.label.count) && (8...500).contains(value.lines.count)
                && value.lines.unicodeScalars.contains { CharacterSet.letters.contains($0) }
                && value.unit.count <= 80 && !text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) && $0 != "\n" }
        }
        var displayLines: String { unit.isEmpty ? lines : unit + "\n" + lines }
    }
    static let addresses = [
        Address(id: "demo-home", label: "Demo home", lines: "123 Example Lane\nAustin, TX 78701, US"),
        Address(id: "demo-office", label: "Demo office", lines: "456 Sample Avenue\nAustin, TX 78701, US"),
    ]
    var addressID = ""
    private var selectedAddress: Address?
    private(set) var stage = Stage.awaitingPayment
    var address: Address? {
        if let selectedAddress, selectedAddress.id == addressID, selectedAddress.isValid { return selectedAddress }
        return Self.addresses.first { $0.id == addressID }
    }

    mutating func select(_ address: Address) {
        guard address.isValid, stage == .awaitingPayment else { return }
        let value = address.normalized
        selectedAddress = value.source == .demo ? nil : value
        addressID = value.id
    }
    mutating func paymentConfirmed() {
        guard stage == .awaitingPayment, address != nil else { return }
        stage = .confirmed
    }
    mutating func advance(paymentIsPaid: Bool) {
        guard paymentIsPaid, address != nil else { return }
        switch stage {
        case .awaitingPayment: paymentConfirmed()
        case .confirmed: stage = .preparing
        case .preparing: stage = .shipped
        case .shipped: stage = .delivered
        case .delivered: break
        }
    }
    func save(orderID: String) throws {
        let data = try JSONEncoder().encode(self)
        let account = "demo.delivery.\(orderID)"
        #if os(watchOS)
        try Keychain.write(data, account: account)
        UserDefaults.standard.removeObject(forKey: account) // remove the earlier fictional-only format after successful migration
        #else
        // Portable checks use fictional fixtures, never a person's address.
        UserDefaults.standard.set(data, forKey: account)
        #endif
    }
    static func load(orderID: String) -> Self {
        let account = "demo.delivery.\(orderID)"
        #if os(watchOS)
        let data = Keychain.read(account: account) ?? UserDefaults.standard.data(forKey: account)
        #else
        let data = UserDefaults.standard.data(forKey: account)
        #endif
        guard let data, let delivery = try? JSONDecoder().decode(Self.self, from: data),
              delivery.addressID.isEmpty || delivery.address != nil else { return Self() }
        return delivery
    }
}

/// Reject cached, invalid or neighborhood-scale fixes instead of implying an exact delivery point.
enum DeliveryLocationFix {
    static func isUsable(accuracy: Double, timestamp: Date, now: Date = Date()) -> Bool {
        accuracy.isFinite && (0...250).contains(accuracy) && (-5...60).contains(now.timeIntervalSince(timestamp))
    }
}

#if os(watchOS)
enum SavedDeliveryAddresses {
    static func scope(token: String) -> String {
        "delivery-addresses.v1." + SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    static func load(scope: String) -> [DemoDelivery.Address] {
        guard let data = Keychain.read(account: scope),
              let values = try? JSONDecoder().decode([DemoDelivery.Address].self, from: data) else { return [] }
        return Array(values.filter { $0.isValid && $0.source != .demo }.prefix(5))
    }
    static func save(_ address: DemoDelivery.Address, scope: String) throws {
        guard address.isValid, address.source != .demo else { throw ApprovalError("Check the address before saving.") }
        var value = address.normalized
        value.source = .saved
        let others = load(scope: scope).filter { $0.id != value.id }
        guard others.count < 5 else { throw ApprovalError("Five addresses are saved. Open one to edit it.") }
        try Keychain.write(JSONEncoder().encode([value] + others), account: scope)
    }

    #if DEBUG && targetEnvironment(simulator)
    static func selfCheck() throws {
        let scope = self.scope(token: "fictional-address-check")
        let original = Keychain.read(account: scope)
        defer {
            if let original { try? Keychain.write(original, account: scope) }
            else { Keychain.remove(account: scope) }
        }
        var sample = DemoDelivery.Address(label: "Example", lines: "123 Example Lane, Demo City", source: .manual)
        try save(sample, scope: scope)
        assert(load(scope: scope).first?.lines == sample.lines)
        sample.unit = "Unit 12"
        try save(sample, scope: scope)
        assert(load(scope: scope).filter { $0.id == sample.id }.count == 1)
        assert(load(scope: scope).first?.unit == "Unit 12")
        assert(load(scope: self.scope(token: "another-fictional-account")).isEmpty)
        let orderID = "fictional-address-order-check"
        let account = "demo.delivery.\(orderID)"
        let oldSecure = Keychain.read(account: account)
        let oldLegacy = UserDefaults.standard.data(forKey: account)
        defer {
            if let oldSecure { try? Keychain.write(oldSecure, account: account) }
            else { Keychain.remove(account: account) }
            if let oldLegacy { UserDefaults.standard.set(oldLegacy, forKey: account) }
            else { UserDefaults.standard.removeObject(forKey: account) }
        }
        var delivery = DemoDelivery()
        delivery.select(sample)
        try delivery.save(orderID: orderID)
        assert(DemoDelivery.load(orderID: orderID).address?.unit == "Unit 12")
        assert(UserDefaults.standard.data(forKey: account) == nil)
    }
    #endif
}
#endif
