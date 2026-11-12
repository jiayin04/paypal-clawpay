import SwiftUI

/// Saved-address selection and one-shot location suggestions always end in user review.
struct DeliveryAddressPicker: View {
    let scope: String
    var manageSaved = false
    let onSelect: (DemoDelivery.Address) -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var location = DeliveryLocation()
    @State private var saved: [DemoDelivery.Address] = []
    @State private var draft: DemoDelivery.Address?
    @State private var locationConsent = false
    @State private var saveError: String?

    var body: some View {
        Group {
            if let draft {
                DeliveryAddressEditor(initial: draft, manageSaved: manageSaved, error: saveError,
                                      onBack: { self.draft = nil; saveError = nil }, onUse: use)
            } else {
                List {
                    if !saved.isEmpty {
                        Section("Saved on this watch") {
                            ForEach(saved) { address in
                                Button {
                                    location.cancel()
                                    saveError = nil
                                    draft = address
                                } label: {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(address.label).font(.headline)
                                        Text(address.displayLines).font(.footnote).lineLimit(2).foregroundStyle(.secondary)
                                    }.privacySensitive()
                                }
                            }
                        }
                    }
                    Section {
                        switch location.phase {
                        case .authorizing: ProgressView("Allow location access…").font(.footnote)
                        case .locating: ProgressView("Finding location…").font(.footnote)
                        case .resolving: ProgressView("Finding address…").font(.footnote)
                        case .failed(let message): Text(message).font(.footnote).foregroundStyle(.secondary)
                        default: EmptyView()
                        }
                        Button {
                            locationConsent = true
                        } label: {
                            Label("Use current location", systemImage: "location")
                        }.disabled(location.isBusy)
                        Button {
                            location.cancel()
                            draft = DemoDelivery.Address(label: "Home", lines: "", source: .manual)
                        } label: {
                            Label("Enter address", systemImage: "pencil")
                        }
                        if location.isBusy { Button("Cancel lookup") { location.cancel() } }
                    } footer: {
                        Text("Choose an address and review it before continuing.")
                    }
                    if !manageSaved {
                        Section("Example addresses") {
                            ForEach(DemoDelivery.addresses) { address in
                                Button(address.label) { location.cancel(); draft = address }
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle(draft == nil ? "Deliver to" : "Review address")
        .containerBackground(Theme.background, for: .navigation)
        .confirmationDialog("Use current location?", isPresented: $locationConsent, titleVisibility: .visible) {
            Button("Find my address") { location.start() }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("Apple Maps receives your location to suggest an address. You review it before use. ClawPay won't send it to the shopping agent or a retailer.")
        }
        .onAppear {
            saved = SavedDeliveryAddresses.load(scope: scope)
            #if DEBUG && targetEnvironment(simulator)
            switch ProcessInfo.processInfo.environment["CLAWPAY_ADDRESS_FIXTURE"] {
            case "review":
                draft = DemoDelivery.Address(label: "Current location", lines: "123 Example Lane\nDemo City, TX 78701, US", source: .currentLocation)
            case "saved":
                saved = [DemoDelivery.Address(label: "Example home", lines: "123 Example Lane\nDemo City, TX 78701, US", source: .saved)]
            case "denied": location.showDeniedFixture()
            case "lookup": location.resolvePublicFixture()
            default: break
            }
            #endif
        }
        .onChange(of: location.phase) {
            if case .ready(var address) = location.phase {
                #if DEBUG && targetEnvironment(simulator)
                if ProcessInfo.processInfo.environment["CLAWPAY_ADDRESS_FIXTURE"] == "lookup" {
                    address.label = "Public test location"
                    NSLog("ClawPay public location fixture resolved")
                }
                #endif
                draft = address
                saveError = nil
            }
        }
        .onChange(of: scenePhase) {
            if scenePhase == .background { location.cancel() }
        }
        .onDisappear { location.cancel() }
    }

    private func use(_ address: DemoDelivery.Address, remember: Bool) {
        let value = address.normalized
        guard value.isValid else { return }
        do {
            if remember || manageSaved { try SavedDeliveryAddresses.save(value, scope: scope) }
            onSelect(value)
            dismiss()
        } catch {
            saveError = error.localizedDescription.hasPrefix("Five addresses") ? error.localizedDescription : "Couldn't save this address. Try again."
        }
    }
}

private struct DeliveryAddressEditor: View {
    let manageSaved: Bool
    let error: String?
    let onBack: () -> Void
    let onUse: (DemoDelivery.Address, Bool) -> Void
    @State private var address: DemoDelivery.Address
    @State private var remember = false

    init(initial: DemoDelivery.Address, manageSaved: Bool, error: String?, onBack: @escaping () -> Void,
         onUse: @escaping (DemoDelivery.Address, Bool) -> Void) {
        self.manageSaved = manageSaved; self.error = error; self.onBack = onBack; self.onUse = onUse
        _address = State(initialValue: initial)
    }

    var body: some View {
        Form {
            Section {
                if address.source == .demo {
                    Text(address.label).font(.headline)
                    Text(address.lines).font(.footnote)
                } else {
                    if !address.lines.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text(address.normalized.displayLines)
                            .font(.footnote).fixedSize(horizontal: false, vertical: true)
                    }
                    TextField("Label, e.g. Home", text: $address.label)
                    TextField("Full street address + postcode", text: $address.lines, axis: .vertical)
                    TextField("Apartment / unit (optional)", text: $address.unit)
                }
            } header: {
                Text(address.source == .currentLocation ? "Location suggestion" : "Delivery address")
            } footer: {
                Text(address.source == .demo ? "Example address. No items will be shipped." : "Check the street, building, postal code and unit. Delivery preview only.")
            }
            if !manageSaved && address.source != .demo {
                Section {
                    Toggle("Remember on this watch", isOn: $remember)
                } footer: {
                    Text("The address stays in this receipt. Remember adds it to saved addresses for future orders.")
                }
            }
            if let error { Text(error).font(.footnote).foregroundStyle(.red) }
            Section {
                Button(manageSaved ? "Save address" : "Use this address") { onUse(address, remember) }
                    .disabled(!address.isValid)
                    .tint(Theme.accent)
                Button("Choose another address", action: onBack)
            }
        }.privacySensitive()
    }
}
