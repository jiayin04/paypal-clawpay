import Foundation

@main
struct VoiceAudioChecks {
    static func main() throws {
        var detector = SpeechEndDetector()
        for step in 1...30 { assert(!detector.shouldStop(power: -70, at: Double(step) / 10)) }
        // A click is not speech; continuous silence never causes a request to be sent.
        assert(!detector.shouldStop(power: -15, at: 3.1))
        for step in 32...50 { assert(!detector.shouldStop(power: -70, at: Double(step) / 10)) }
        detector = SpeechEndDetector()
        for step in 1...6 { assert(!detector.shouldStop(power: -20, at: Double(step) / 10)) }
        for step in 7...15 { assert(!detector.shouldStop(power: -65, at: Double(step) / 10)) }
        assert(detector.shouldStop(power: -65, at: 1.8))
        // Speaking again before the pause completes extends the turn.
        detector = SpeechEndDetector()
        for step in 1...6 { _ = detector.shouldStop(power: -20, at: Double(step) / 10) }
        _ = detector.shouldStop(power: -65, at: 1.2)
        assert(!detector.shouldStop(power: -20, at: 1.4))
        assert(!detector.shouldStop(power: -65, at: 2.0))
        assert(detector.shouldStop(power: -65, at: 2.6))
        detector = SpeechEndDetector()
        assert(!detector.shouldStop(power: -20, at: 0.1))
        assert(!detector.shouldStop(power: -20, at: 0.2))
        assert(detector.shouldStop(power: -65, at: 1.4)) // a short "yes" ends normally
        var pcm = PCMStreamBuffer()
        let first = try pcm.decode(Data([0]))
        assert(first == [])
        let second = try pcm.decode(Data([0, 0xff, 0x7f, 0, 0x80]))
        assert(second == [0, Float(32767) / 32768, -1])
        try pcm.finish()
        for input in [Data(), Data([1])] {
            var invalid = PCMStreamBuffer()
            _ = try invalid.decode(input)
            do { try invalid.finish(); assertionFailure("Invalid PCM accepted") } catch { }
        }
        var oversized = PCMStreamBuffer()
        do { _ = try oversized.decode(Data(repeating: 0, count: PCMStreamBuffer.maxBytes + 1)); assertionFailure("Unbounded audio accepted") } catch { }
        var delivery = DemoDelivery()
        delivery.paymentConfirmed()
        delivery.advance(paymentIsPaid: true)
        assert(delivery.stage == .awaitingPayment) // no destination, no fulfillment
        delivery.addressID = "demo-home"
        delivery.advance(paymentIsPaid: false)
        assert(delivery.stage == .awaitingPayment)
        delivery.paymentConfirmed()
        assert(delivery.stage == .confirmed)
        delivery.advance(paymentIsPaid: true)
        assert(delivery.stage == .preparing)
        delivery.advance(paymentIsPaid: true)
        assert(delivery.stage == .shipped)
        delivery.advance(paymentIsPaid: true)
        assert(delivery.stage == .delivered)
        delivery.advance(paymentIsPaid: true)
        assert(delivery.stage == .delivered)
        let demoID = "voice-checks-fictional-order"
        try delivery.save(orderID: demoID)
        assert(DemoDelivery.load(orderID: demoID) == delivery)
        UserDefaults.standard.removeObject(forKey: "demo.delivery.\(demoID)")
        var address = DemoDelivery.Address(label: " Home ", lines: " 123 Example Lane \n Demo City 78701 ", unit: " Unit 12 ", source: .manual)
        assert(address.isValid)
        assert(address.normalized.label == "Home")
        assert(address.normalized.displayLines == "Unit 12\n123 Example Lane\nDemo City 78701")
        var custom = DemoDelivery()
        custom.select(address)
        assert(custom.address?.unit == "Unit 12" && custom.address?.source == .manual)
        custom.paymentConfirmed()
        address.id = "a-different-address"
        custom.select(address)
        assert(custom.addressID != address.id) // the destination stays fixed after payment
        let restored = try JSONDecoder().decode(DemoDelivery.self, from: JSONEncoder().encode(custom))
        assert(restored == custom)
        let legacy = Data("{\"addressID\":\"demo-home\",\"stage\":\"confirmed\"}".utf8)
        let migrated = try JSONDecoder().decode(DemoDelivery.self, from: legacy)
        assert(migrated.address?.label == "Demo home")
        address.lines = "   "
        assert(!address.isValid)
        address.lines = String(repeating: "x", count: 501)
        assert(!address.isValid)
        address.lines = "123 Example\0Lane"
        assert(!address.isValid)
        address.lines = "30.2672, -97.7431"
        assert(!address.isValid) // raw coordinates are not a delivery address
        let now = Date()
        assert(DeliveryLocationFix.isUsable(accuracy: 25, timestamp: now, now: now))
        assert(!DeliveryLocationFix.isUsable(accuracy: 1000, timestamp: now, now: now))
        assert(!DeliveryLocationFix.isUsable(accuracy: -1, timestamp: now, now: now))
        assert(!DeliveryLocationFix.isUsable(accuracy: .nan, timestamp: now, now: now))
        assert(!DeliveryLocationFix.isUsable(accuracy: 10, timestamp: now.addingTimeInterval(-61), now: now))
        assert(!DeliveryLocationFix.isUsable(accuracy: 10, timestamp: now.addingTimeInterval(6), now: now))
        print("Voice, delivery, address validation and location-fix checks passed.")
    }
}
