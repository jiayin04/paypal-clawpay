import CoreLocation
import MapKit
import Observation

/// One foreground fix after the user chooses location. Nothing is logged, persisted or sent to the LLM.
@MainActor
@Observable
final class DeliveryLocation: NSObject, CLLocationManagerDelegate {
    enum Phase: Equatable {
        case idle, authorizing, locating, resolving
        case ready(DemoDelivery.Address), failed(String)
    }
    private(set) var phase = Phase.idle
    @ObservationIgnored private var manager: CLLocationManager?
    @ObservationIgnored private var work: Task<Void, Never>?
    @ObservationIgnored private var timeout: Task<Void, Never>?
    @ObservationIgnored private var cancelGeocode: (() -> Void)?
    @ObservationIgnored private var generation = 0

    var isBusy: Bool { phase == .authorizing || phase == .locating || phase == .resolving }

    func start() {
        cancel()
        let manager = CLLocationManager()
        self.manager = manager
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        manager.delegate = self
        switch manager.authorizationStatus {
        case .notDetermined:
            phase = .authorizing
            manager.requestWhenInUseAuthorization()
        case .authorizedAlways, .authorizedWhenInUse: locate()
        default: fail("Location is unavailable. Enable it in Settings, or use a saved address.")
        }
    }

    func cancel() {
        generation += 1
        work?.cancel(); work = nil
        timeout?.cancel(); timeout = nil
        cancelGeocode?(); cancelGeocode = nil
        manager?.delegate = nil
        manager?.stopUpdatingLocation()
        manager = nil
        phase = .idle
    }

    private func fail(_ message: String) {
        cancel()
        phase = .failed(message)
    }

    private func locate() {
        guard let manager, phase != .locating, phase != .resolving else { return }
        phase = .locating
        armTimeout()
        manager.requestLocation()
    }

    private func armTimeout() {
        guard timeout == nil else { return }
        let current = generation
        timeout = Task {
            do { try await Task.sleep(for: .seconds(20)) } catch { return }
            guard current == generation else { return }
            fail("Couldn't find an address in time. Try again or enter it manually.")
        }
    }

    private func resolve(_ location: CLLocation) {
        guard CLLocationCoordinate2DIsValid(location.coordinate),
              DeliveryLocationFix.isUsable(accuracy: location.horizontalAccuracy, timestamp: location.timestamp) else {
            fail("Location is too approximate. Enter or choose your delivery address.")
            return
        }
        manager?.delegate = nil
        manager?.stopUpdatingLocation()
        manager = nil
        phase = .resolving
        armTimeout()
        let current = generation
        work = Task {
            defer { if current == generation { work = nil } }
            do {
                let address: String
                if #available(watchOS 26.0, *) {
                    guard let request = MKReverseGeocodingRequest(location: location) else { throw ApprovalError("Address unavailable.") }
                    cancelGeocode = { request.cancel() }
                    let items = try await request.mapItems
                    address = items.first?.address?.fullAddress ?? ""
                } else {
                    let geocoder = CLGeocoder()
                    cancelGeocode = { geocoder.cancelGeocode() }
                    let placemarks = try await geocoder.reverseGeocodeLocation(location)
                    let place = placemarks.first
                    let street = [place?.subThoroughfare, place?.thoroughfare].compactMap { $0 }.joined(separator: " ")
                    let city = [place?.locality, place?.administrativeArea, place?.postalCode].compactMap { $0 }.joined(separator: " ")
                    address = [street, city, place?.country ?? ""].filter { !$0.isEmpty }.joined(separator: "\n")
                }
                guard current == generation, !Task.isCancelled else { return }
                let draft = DemoDelivery.Address(label: "Current location", lines: address, source: .currentLocation).normalized
                guard draft.isValid else { throw ApprovalError("Address unavailable.") }
                timeout?.cancel(); timeout = nil
                cancelGeocode = nil
                phase = .ready(draft)
            } catch {
                guard current == generation, !Task.isCancelled else { return }
                fail("Couldn't resolve this location. Enter your address instead.")
            }
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            guard manager === self.manager else { return }
            switch manager.authorizationStatus {
            case .authorizedAlways, .authorizedWhenInUse: self.locate()
            case .denied, .restricted: self.fail("Location access is off. Use a saved address or enter one.")
            default: break
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        Task { @MainActor in
            guard manager === self.manager, self.phase == .locating, let location = locations.last else { return }
            self.resolve(location)
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in
            guard manager === self.manager else { return }
            self.fail("Couldn't get your location. Use a saved address or enter one.")
        }
    }

    #if DEBUG && targetEnvironment(simulator)
    /// Public Austin test coordinates; this never reads or changes the watch's location/permission.
    func resolvePublicFixture() {
        cancel()
        resolve(CLLocation(coordinate: CLLocationCoordinate2D(latitude: 30.2672, longitude: -97.7431),
                           altitude: 0, horizontalAccuracy: 10, verticalAccuracy: 10, timestamp: Date()))
    }
    func showDeniedFixture() {
        fail("Location access is off. Use a saved address or enter one.")
    }
    #endif
}
