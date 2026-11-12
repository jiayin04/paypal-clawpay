import SwiftUI
import WatchKit

struct ChatLine: Identifiable, Equatable {
    let id = UUID()
    let fromUser: Bool
    let text: String
}

struct PendingOrder: Equatable {
    enum State: Equatable {
        case awaiting, working, paid, processing
        case confirming(PreparedApproval)  // verified challenge, waiting for the user's Confirm
        case failed(String)
    }

    let orderId: String
    let total: String  // from the chat reply; replaced by the signed amount once verified
    let merchant: String?
    var signedTotal: String?
    var items: [AssistantReply.Item] = []
    var paidAt: String?
    var state: State = .awaiting

    var displayTotal: String { signedTotal ?? total }
}

@MainActor
struct ContentView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    // Editable in Settings; simulator builds can also seed them from SIMCTL_CHILD_CLAWPAY_* variables.
    @AppStorage("baseURL") private var baseURL = "http://127.0.0.1:8000"
    @State private var deviceToken = ""  // kept in the Keychain, not UserDefaults
    // On only when deviceToken is this watch's own registered device (scripts/register_watch.py).
    @AppStorage("approveOnWatch") private var approveOnWatch = false
    @AppStorage("lastOrderID") private var lastOrderID = ""

    @State private var lines: [ChatLine] = []
    @State private var pendingOrder: PendingOrder?
    @State private var busy = false
    @State private var resetNext = false
    @State private var seeded = false
    @State private var speaker = SpeechPlayer()
    @State private var approverKey: ApproverKey?
    @State private var keyError: String?
    @State private var autoApprove = false
    @State private var showingPayment = false
    @State private var showingHistory = false
    @State private var showingMicrophone = false
    @State private var capture = SpeechCapture()
    @State private var products: [AssistantReply.Product] = []
    @State private var progress: [AgentProgress] = []
    @State private var demoDelivery = DemoDelivery()
    @State private var receiptError: String?
    @State private var refreshingReceipt = false

    private let suggestions = ["Shop", "Spending"]

    @ViewBuilder var body: some View {
        #if DEBUG && targetEnvironment(simulator)
        if ProcessInfo.processInfo.environment["CLAWPAY_REVIEW_SCREEN"] == "address" {
            NavigationStack {
                DeliveryAddressPicker(scope: SavedDeliveryAddresses.scope(token: "fictional-address-review")) { _ in }
            }.modifier(WatchReviewOptions())
        } else { appBody }
        #else
        appBody
        #endif
    }

    private var appBody: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if lines.isEmpty && pendingOrder == nil && !busy {
                            HeroView(suggestions: suggestions, onPick: send, onTalk: startMicrophone)
                        }
                        ForEach(lines.suffix(busy ? 0 : 1)) { line in
                            ChatBubble(line: line,
                                       speaking: speaker.isSpeaking && line.id == lines.last?.id && !line.fromUser)
                                .id(line.id)
                        }
                        if busy {
                            TypingBubble(steps: progress)
                                .id("thinking")
                        }
                        if let voiceError = speaker.lastError {
                            Label(voiceError, systemImage: "speaker.slash")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if !busy {
                            ForEach(products.prefix(2)) { product in
                                Button {
                                    send("Order one \(product.name) from \(product.merchantId), SKU \(product.sku).")
                                } label: {
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(product.name).font(.footnote).lineLimit(3)
                                        HStack {
                                            Text(product.price).font(.headline).monospacedDigit()
                                            Spacer()
                                            Image(systemName: "plus.circle.fill")
                                        }
                                    }
                                    .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                                    .background(Theme.surface, in: RoundedRectangle(cornerRadius: 14))
                                }.buttonStyle(.plain)
                                    .accessibilityHint("Prepare a cart for this product; payment needs confirmation")
                            }
                        }
                        if let order = pendingOrder {
                            Button { showingPayment = true } label: {
                                VStack(alignment: .leading, spacing: 6) {
                                    Label(order.state == .paid ? "Payment receipt" : "View payment", systemImage: "creditcard")
                                        .font(.footnote)
                                    Text(order.displayTotal).font(.headline).monospacedDigit()
                                    if let item = order.items.first {
                                        Text("\(item.qty) × \(item.name)").font(.footnote).lineLimit(2)
                                    }
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(12)
                                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                            }
                            .buttonStyle(.plain)
                        }
                        if !progress.isEmpty, !busy {
                            NavigationLink { AgentActivityView(steps: progress) } label: {
                                Label("Agent activity", systemImage: "list.bullet.rectangle")
                                    .font(.footnote)
                            }.buttonStyle(.plain).frame(minHeight: 44)
                        }
                        if !lines.isEmpty || pendingOrder != nil || busy {
                            MicButton(busy: busy, action: startMicrophone)
                                .padding(.top, 4)
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .scenePadding(.horizontal)
                    .padding(.top, 8)
                    .padding(.bottom, 8)
                    .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: lines)
                    .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: pendingOrder)
                }
                .onChange(of: lines) {
                    if let last = lines.last { scroll(proxy, to: last.id) }
                }
                .onChange(of: busy) {
                    if busy { scroll(proxy, to: "thinking") }
                    else { consumeVoiceRequest() }
                }
                .onChange(of: pendingOrder?.orderId) {
                    showingPayment = pendingOrder != nil
                }
                .onChange(of: deviceToken) { _, current in
                    do { try DeviceToken.save(current) }
                    catch { lines.append(ChatLine(fromUser: false, text: "Couldn't save the device token. Re-enter it in Settings.")) }
                }
                .onChange(of: client) { previous, current in
                    guard previous != nil, previous != current else { return }
                    speaker.stop()
                    capture.cancel()
                    showingMicrophone = false
                    showingPayment = false
                    pendingOrder = nil
                    lastOrderID = ""
                    demoDelivery = DemoDelivery()
                    lines = []
                    products = []
                    progress = []
                    busy = false
                    refreshingReceipt = false
                    receiptError = nil
                    resetNext = true
                }
            }
            .containerBackground(Theme.background, for: .navigation)
            .navigationTitle("ClawPay")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    NavigationLink {
                        SettingsView(baseURL: $baseURL, deviceToken: $deviceToken,
                                     approveOnWatch: $approveOnWatch, lastVoice: speaker.lastVoice,
                                     approverKey: approverKey, keyError: keyError)
                    } label: {
                        Image(systemName: "gearshape")
                            .foregroundStyle(.white)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showingHistory = true } label: {
                        Image(systemName: "clock.arrow.circlepath")
                            .foregroundStyle(.white)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Conversation history")
                    .disabled(lines.isEmpty || busy)
                }
            }
            .navigationDestination(isPresented: $showingHistory) {
                ConversationHistoryView(lines: lines, onNewChat: {
                    showingHistory = false
                    newConversation()
                })
            }
            .tint(Theme.accent)
            .onChange(of: capture.phase) { consumeCapture() }
            .sheet(isPresented: $showingMicrophone, onDismiss: {
                capture.cancel()
                consumeVoiceRequest()
            }) {
                SpeechInputView(capture: capture, onRetry: startRecording, onCancel: { showingMicrophone = false })
            }
            .sheet(isPresented: $showingPayment, onDismiss: {
                if pendingOrder?.isConfirming == true { pendingOrder?.state = .awaiting }
            }) {
                NavigationStack {
                    ScrollView {
                        if let order = pendingOrder {
                            ApprovalCard(order: order, approveOnWatch: approveOnWatch, onApprove: approve,
                                         onConfirm: confirm, onCancel: {
                                             pendingOrder?.state = .awaiting
                                             showingPayment = false
                                         }, delivery: $demoDelivery, onDeliveryChanged: saveDelivery,
                                         onAdvanceDelivery: advanceDelivery, onRefresh: { Task { await refreshReceipt() } },
                                         addressScope: SavedDeliveryAddresses.scope(token: deviceToken))
                                .disabled(busy)
                                .scenePadding(.horizontal)
                                .padding(.vertical, 8)
                            if refreshingReceipt { ProgressView("Updating receipt…").font(.footnote) }
                            if let receiptError {
                                Text(receiptError).font(.footnote).foregroundStyle(.secondary).scenePadding(.horizontal)
                            }
                        }
                    }
                    .id(pendingOrder?.isConfirming)
                    .navigationTitle("Payment")
                    .containerBackground(Theme.background, for: .navigation)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Done") { showingPayment = false }
                        }
                    }
                }
                #if DEBUG && targetEnvironment(simulator)
                .modifier(WatchReviewOptions())
                #endif
            }
        }
        .onAppear(perform: seedFromEnvironment)
        .onReceive(NotificationCenter.default.publisher(for: VoiceRequestInbox.changed)) { _ in consumeVoiceRequest() }
        .onChange(of: scenePhase) {
            if scenePhase == .active { consumeCapture(); consumeVoiceRequest() }
            if scenePhase == .background, showingMicrophone {
                capture.cancel()
                showingMicrophone = false
            }
        }
        .onChange(of: showingPayment) { if !showingPayment { consumeVoiceRequest() } }
        .onChange(of: pendingOrder?.state) { consumeVoiceRequest() }
        .task(id: scenePhase == .active && pendingOrder?.state == .processing ? pendingOrder?.orderId : nil) {
            guard pendingOrder?.state == .processing else { return }
            for _ in 0..<12 {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                guard !Task.isCancelled, scenePhase == .active, pendingOrder?.state == .processing else { return }
                await refreshReceipt()
            }
        }
        #if DEBUG && targetEnvironment(simulator)
        .modifier(WatchReviewOptions())
        #endif
    }

    private var client: AssistantClient? {
        guard let url = URL(string: baseURL) else { return nil }
        return AssistantClient(baseURL: url, token: deviceToken)  // nil for plain HTTP off this machine
    }

    private func scroll<ID: Hashable>(_ proxy: ScrollViewProxy, to id: ID) {
        withAnimation(reduceMotion ? nil : .smooth(duration: 0.25)) {
            proxy.scrollTo(id, anchor: .top)
        }
    }

    private func seedFromEnvironment() {
        guard !seeded else { return }
        seeded = true
        deviceToken = DeviceToken.load()
        loadApproverKey()
        #if targetEnvironment(simulator)
        // Demo and test hooks, compiled into simulator builds only: a real watch is configured in Settings.
        let env = ProcessInfo.processInfo.environment
        if let url = env["CLAWPAY_BASE_URL"], !url.isEmpty { baseURL = url }
        if let token = env["CLAWPAY_TOKEN"], !token.isEmpty { deviceToken = token }
        if let flag = env["CLAWPAY_APPROVE_ON_WATCH"], !flag.isEmpty { approveOnWatch = flag == "1" }
        #if DEBUG
        if env["CLAWPAY_ADDRESS_SELF_CHECK"] == "1" {
            do {
                try SavedDeliveryAddresses.selfCheck()
                NSLog("ClawPay saved-address Keychain checks passed")
            } catch { assertionFailure("Saved-address Keychain checks failed") }
        }
        if env["CLAWPAY_VOICE_SELF_CHECK"] == "1" {
            do {
                try VoiceRequestInbox.selfCheck()
                NSLog("ClawPay voice inbox self-check passed")
            } catch {
                assertionFailure("Voice inbox self-check failed")
            }
        }
        // Local visual-review fixtures never contact the backend or sign a payment.
        if let screen = env["CLAWPAY_REVIEW_SCREEN"], let review = WatchReviewScreen(rawValue: screen) {
            review.apply(lines: &lines, order: &pendingOrder, busy: &busy)
            if review == .loading {
                progress = [AgentProgress(stage: "assistant", message: "Understood your request.", status: "complete"),
                            AgentProgress(stage: "search_products", message: "Searching Channel3…", status: "working")]
            }
            if review == .confirmation || review == .paid {
                demoDelivery.addressID = "demo-home"
                if review == .paid { demoDelivery.paymentConfirmed() }
            }
            approveOnWatch = true
            return
        }
        if env["CLAWPAY_RECORDING_FRESH_START"] == "1" {
            // Clear the opening screen, preserving credentials and saved receipts.
            lastOrderID = ""
            newConversation()
            return
        }
        if let orderID = env["CLAWPAY_RECEIPT_ORDER_ID"], UUID(uuidString: orderID) != nil {
            lastOrderID = orderID
        }
        #endif
        autoApprove = env["CLAWPAY_DEMO_APPROVE"] == "1"  // taps Approve and Confirm automatically
        if let prompt = env["CLAWPAY_DEMO_PROMPT"], !prompt.isEmpty { send(prompt) }
        #if DEBUG
        if let prompt = env["CLAWPAY_DEMO_SIRI_REQUEST"], !prompt.isEmpty {
            Task {
                do {
                    let intent = AskClawPayIntent()
                    intent.request = prompt
                    _ = try await intent.perform()
                    NSLog("ClawPay Siri intent handler queued request")
                } catch {
                    lines.append(ChatLine(fromUser: false, text: error.localizedDescription))
                }
            }
        }
        if let path = env["CLAWPAY_TRANSCRIBE_FIXTURE"], let client {
            showingMicrophone = true
            capture.transcribeFixture(URL(filePath: path), client: client)
        }
        #endif
        #endif
        consumeVoiceRequest()
        if !lastOrderID.isEmpty, pendingOrder == nil, !busy { Task { await refreshReceipt() } }
    }

    private func consumeVoiceRequest() {
        guard seeded, scenePhase == .active, !busy, !showingMicrophone else { return }
        if pendingOrder?.isConfirming == true || pendingOrder?.state == .working { return }
        guard let request = VoiceRequestInbox.take() else { return }
        showingPayment = false
        showingHistory = false
        send(request)
    }

    private func consumeCapture() {
        guard showingMicrophone, scenePhase == .active, case .ready(let text) = capture.phase else { return }
        capture.cancel()  // consume before send: SwiftUI updates cannot submit the same transcript twice
        showingMicrophone = false
        #if DEBUG && targetEnvironment(simulator)
        NSLog("ClawPay voice transcript submitted")
        #endif
        send(text)
    }

    private func startMicrophone() {
        guard !busy, !showingMicrophone, !showingPayment else { return }
        guard client != nil else {
            lines.append(ChatLine(fromUser: false, text: "Connect this watch in Settings first."))
            return
        }
        speaker.stop()
        showingMicrophone = true
        startRecording()
    }

    private func startRecording() {
        guard let client else { return }
        Task { await capture.start(client: client) }
    }

    private func newConversation() {
        speaker.stop()
        lines = []
        pendingOrder = nil
        products = []
        progress = []
        resetNext = true
    }

    private func send(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        guard let client else {
            lines.append(ChatLine(fromUser: false, text: "Connect this watch in Settings first."))
            return
        }
        speaker.stop()
        let prompt = switch text {
        case "Shop", "Find products": "Help me shop on Channel3. Ask what I need."
        case "Spending", "Spending today": "What did I spend today?"
        default: text
        }
        lines.append(ChatLine(fromUser: true, text: text))
        busy = true
        products = []
        progress = []
        let reset = resetNext
        resetNext = false
        Task {
            do {
                let answer = try await client.sendStreaming(prompt, reset: reset, approver: approveOnWatch ? "watch" : "device") { step in
                    guard self.client == client else { return }
                    #if DEBUG && targetEnvironment(simulator)
                    NSLog("ClawPay agent progress received")
                    #endif
                    if let last = progress.last, last.stage == step.stage {
                        progress[progress.count - 1] = step
                    } else {
                        if let last = progress.last, last.status == "working" {
                            progress[progress.count - 1] = AgentProgress(stage: last.stage, message: last.message, status: "complete")
                        }
                        progress.append(step)
                    }
                }
                guard self.client == client else { return }
                busy = false
                #if DEBUG && targetEnvironment(simulator)
                NSLog("ClawPay agent stream completed")
                #endif
                if let last = progress.last, last.status == "working" {
                    progress[progress.count - 1] = AgentProgress(stage: last.stage, message: last.message, status: "complete")
                }
                lines.append(ChatLine(fromUser: false, text: answer.reply))
                products = answer.products ?? []
                // Only structured tool results update payment state; never infer it from spoken text.
                if let order = answer.order { updateOrder(order); products = [] }
                WKInterfaceDevice.current().play(answer.approvalRequired ? .notification : .click)
                if autoApprove, approveOnWatch, answer.approvalRequired {
                    approve()
                }
                await speaker.speak(answer.reply, client: client)
            } catch {
                guard self.client == client else { return }
                busy = false
                if let last = progress.last, last.status == "working" {
                    progress[progress.count - 1] = AgentProgress(stage: last.stage, message: "The request was interrupted.", status: "failed")
                }
                lines.append(ChatLine(fromUser: false, text: error.localizedDescription))
                WKInterfaceDevice.current().play(.failure)
            }
        }
    }

    private func updateOrder(_ order: AssistantReply.Order) {
        let state: PendingOrder.State
        switch order.status {
        case "awaiting_device_approval": state = .awaiting
        case "paid": state = .paid
        case "approved", "payment_pending", "requires_reconciliation": state = .processing
        case "payment_failed": state = .failed("Payment failed. Check status before retrying.")
        case "expired", "cancelled", "denied": pendingOrder = nil; return
        default: return
        }
        let previous = pendingOrder?.orderId == order.orderId ? pendingOrder : nil
        if previous == nil { demoDelivery = DemoDelivery.load(orderID: order.orderId) }
        pendingOrder = PendingOrder(orderId: order.orderId, total: order.total ?? previous?.total ?? "Your order",
                                    merchant: order.merchant ?? previous?.merchant,
                                    signedTotal: previous?.signedTotal, items: order.items ?? previous?.items ?? [],
                                    paidAt: order.paidAt ?? previous?.paidAt, state: state)
        lastOrderID = order.orderId
        if state == .paid { demoDelivery.paymentConfirmed(); saveDelivery() }
    }

    private func saveDelivery() {
        guard let order = pendingOrder else { return }
        do { try demoDelivery.save(orderID: order.orderId) }
        catch { receiptError = "Couldn't save the delivery address on this watch. Try again." }
    }

    private func advanceDelivery() {
        guard let order = pendingOrder else { return }
        demoDelivery.advance(paymentIsPaid: order.state == .paid)
        saveDelivery()
        WKInterfaceDevice.current().play(.click)
    }

    private func refreshReceipt() async {
        guard !refreshingReceipt, let client else { return }
        let orderID = pendingOrder?.orderId ?? lastOrderID
        guard !orderID.isEmpty else { return }
        refreshingReceipt = true
        receiptError = nil
        defer { if self.client == client { refreshingReceipt = false } }
        do {
            let receipt = try await client.receipt(orderID: orderID)
            guard self.client == client, orderID == (pendingOrder?.orderId ?? lastOrderID) else { return }
            updateOrder(receipt)
        } catch {
            guard self.client == client, orderID == (pendingOrder?.orderId ?? lastOrderID) else { return }
            if (error as? AssistantError)?.code == "order_not_found" {
                lastOrderID = ""
            } else { receiptError = "Receipt couldn't refresh. Check status again." }
        }
    }
}

extension ContentView {
    private func loadApproverKey() {
        do {
            let key = try ApproverKey.loadOrCreate()
            approverKey = key
            // Public key only, for scripts/register_watch.py --simulator.
            let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            try? key.publicKeyPEM.write(to: docs.appending(path: "approver_public_key.pem"), atomically: true, encoding: .utf8)
        } catch {
            keyError = error.localizedDescription
        }
    }

    /// Step 1: fetch and verify the challenge, then show its signed amount for confirmation.
    private func approve() {
        guard let order = pendingOrder, order.state == .awaiting || order.isRetryable,
              let client, approverKey != nil else {
            if approverKey == nil { pendingOrder?.state = .failed(keyError ?? "No approval key on this watch.") }
            return
        }
        guard demoDelivery.address != nil else { return }
        pendingOrder?.state = .working
        Task {
            do {
                let prepared = try await client.prepareApproval(orderId: order.orderId)
                guard self.client == client, pendingOrder?.orderId == order.orderId else { return }
                pendingOrder?.signedTotal = prepared.payload.display
                pendingOrder?.state = .confirming(prepared)
                WKInterfaceDevice.current().play(.click)
                if autoApprove {
                    autoApprove = false
                    confirm()
                }
            } catch {
                guard self.client == client else { return }
                fail(order, error)
            }
        }
    }

    /// Step 2: the user confirmed the signed amount; presence check, sign in the Secure Enclave, submit.
    private func confirm() {
        guard let order = pendingOrder, case .confirming(let prepared) = order.state,
              let client, let key = approverKey, demoDelivery.address != nil else { return }
        pendingOrder?.state = .working
        Task {
            do {
                let result = try await client.submit(prepared, with: key)
                guard self.client == client, pendingOrder?.orderId == order.orderId else { return }
                switch result.status {
                case "paid":
                    pendingOrder?.state = .paid
                    demoDelivery.paymentConfirmed()
                    saveDelivery()
                    lines.append(ChatLine(fromUser: false, text: "Sandbox payment complete. Your receipt is ready."))
                    WKInterfaceDevice.current().play(.success)
                case "payment_pending":
                    pendingOrder?.state = .processing
                    WKInterfaceDevice.current().play(.success)
                default:
                    pendingOrder?.state = .failed(result.message)
                    WKInterfaceDevice.current().play(.failure)
                }
                Task { await refreshReceipt() }
                await speaker.speak(result.status == "paid" ? "Paid \(prepared.payload.display)." : result.message,
                                    client: client)
            } catch {
                guard self.client == client else { return }
                fail(order, error)
            }
        }
    }

    private func fail(_ order: PendingOrder, _ error: Error) {
        guard pendingOrder?.orderId == order.orderId else { return }
        // The approval already went through (e.g. its response was lost and this is a retry):
        // never claim "not paid" here; the order status says what happened.
        if let code = (error as? AssistantError)?.code, ["challenge_consumed", "invalid_order_status"].contains(code) {
            pendingOrder?.state = .processing
            return
        }
        pendingOrder?.state = .failed(error.localizedDescription)
        WKInterfaceDevice.current().play(.failure)
    }
}

extension PendingOrder {
    var isConfirming: Bool {
        if case .confirming = state { return true }
        return false
    }

    /// A cancelled prompt or network error can be retried; the server keeps the challenge open.
    var isRetryable: Bool {
        if case .failed = state { return true }
        return false
    }
}

// MARK: - Pieces

private struct ConversationHistoryView: View {
    let lines: [ChatLine]
    let onNewChat: () -> Void

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 16) {
                ForEach(lines) { ChatBubble(line: $0, speaking: false) }
            }
            .scenePadding(.horizontal)
        }
        .navigationTitle("History")
        .containerBackground(Theme.background, for: .navigation)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: onNewChat) { Image(systemName: "square.and.pencil") }
                    .accessibilityLabel("New chat")
            }
        }
    }
}

private struct HeroView: View {
    let suggestions: [String]
    let onPick: (String) -> Void
    let onTalk: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            MicButton(busy: false, action: onTalk)
            ForEach(suggestions, id: \.self) { suggestion in
                Button { onPick(suggestion) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: suggestion == "Shop" ? "magnifyingglass" : "chart.bar")
                            .frame(width: 18)
                            .foregroundStyle(.secondary)
                        Text(suggestion).font(.footnote)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 12)
                    .frame(minHeight: 38)
                    .background(Theme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ChatBubble: View {
    let line: ChatLine
    let speaking: Bool

    var body: some View {
        HStack(alignment: .bottom, spacing: 0) {
            if line.fromUser { Spacer(minLength: 12) }
            VStack(alignment: .leading, spacing: 6) {
                Text(line.text)
                    .font(line.fromUser ? .footnote : .body)
                    .fixedSize(horizontal: false, vertical: true)
                if speaking {
                    Label("Speaking", systemImage: "waveform")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, line.fromUser ? 12 : 0)
            .padding(.vertical, line.fromUser ? 10 : 2)
            .foregroundStyle(line.fromUser ? .white.opacity(0.85) : .white)
            .background {
                if line.fromUser {
                    RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.surface)
                }
            }
            if !line.fromUser { Spacer(minLength: 0) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(line.fromUser ? "You" : "ClawPay"): \(line.text)")
        .transition(.opacity)
    }
}

private struct TypingBubble: View {
    let steps: [AgentProgress]
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if steps.isEmpty { ProgressView("Connecting…").font(.footnote) }
            ForEach(Array(steps.suffix(3).enumerated()), id: \.offset) { _, step in
                HStack(alignment: .top, spacing: 7) {
                    if step.status == "working" { ProgressView().controlSize(.mini).frame(width: 18, height: 18) }
                    else { Image(systemName: step.status == "failed" ? "exclamationmark.circle" : "checkmark.circle") }
                    Text(step.message).font(.footnote).fixedSize(horizontal: false, vertical: true)
                }.foregroundStyle(.secondary)
            }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
    }
}

private struct AgentActivityView: View {
    let steps: [AgentProgress]
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("Actions taken").font(.headline)
                ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                    Label(step.message, systemImage: step.status == "failed" ? "exclamationmark.circle" : "checkmark.circle")
                        .font(.footnote).fixedSize(horizontal: false, vertical: true)
                }
                Text("Completed agent actions and order updates.")
                    .font(.caption).foregroundStyle(.secondary)
            }.scenePadding(.horizontal)
        }.navigationTitle("Agent activity")
            .containerBackground(Theme.background, for: .navigation)
    }
}

private struct ApprovalCard: View {
    let order: PendingOrder
    let approveOnWatch: Bool
    let onApprove: () -> Void
    let onConfirm: () -> Void
    let onCancel: () -> Void
    @Binding var delivery: DemoDelivery
    let onDeliveryChanged: () -> Void
    let onAdvanceDelivery: () -> Void
    let onRefresh: () -> Void
    let addressScope: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Text(order.displayTotal)
                .font(.title2.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.white)
                .fixedSize(horizontal: false, vertical: true)
            if let merchant = order.merchant, !order.isConfirming {
                Text(merchant).font(.footnote).foregroundStyle(.secondary)
            }
            if !order.items.isEmpty {
                Divider()
                ForEach(Array(order.items.enumerated()), id: \.offset) { _, item in
                    VStack(alignment: .leading, spacing: 3) {
                        Text("\(item.qty) × \(item.name)").font(.footnote).fixedSize(horizontal: false, vertical: true)
                        Text(item.lineTotal).font(.caption).monospacedDigit().foregroundStyle(.secondary)
                    }
                }
            }
            if order.state == .awaiting || order.isConfirming || (order.state == .paid && delivery.address == nil) {
                Divider()
                Text("Deliver to").font(.caption).foregroundStyle(.secondary)
                NavigationLink {
                    DeliveryAddressPicker(scope: addressScope) { address in
                        delivery.select(address)
                        if order.state == .paid { delivery.paymentConfirmed() }
                        onDeliveryChanged()
                    }
                } label: {
                    Label(delivery.address?.label ?? "Choose delivery address", systemImage: "mappin.and.ellipse")
                        .font(.footnote).frame(minHeight: 44)
                }.disabled(order.isConfirming)
                if let address = delivery.address {
                    Text(address.displayLines).font(.caption).foregroundStyle(.secondary).privacySensitive()
                    Text(address.source == .demo ? "Example address. No items will be shipped." : "Address stays on this watch. Delivery preview only.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            footer
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    @ViewBuilder private var header: some View {
        switch order.state {
        case .paid:
            Label("Sandbox paid", systemImage: "checkmark.circle.fill").font(.caption.weight(.semibold)).foregroundStyle(Theme.paid)
        case .processing:
            Label("Processing", systemImage: "clock").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
        case .failed:
            Label("Needs attention", systemImage: "exclamationmark.circle").font(.caption.weight(.semibold)).foregroundStyle(.red)
        case .confirming:
            Label("Confirm", systemImage: "lock.shield").font(.caption.weight(.semibold))
                .foregroundStyle(.primary)
        default:
            Label(approveOnWatch ? "Review payment" : "Approve on ClawPay",
                  systemImage: approveOnWatch ? "lock.shield" : "hand.tap")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.primary)
        }
    }

    @ViewBuilder private var footer: some View {
        switch order.state {
        case .paid:
            VStack(alignment: .leading, spacing: 10) {
                Text("PayPal Sandbox payment complete.").font(.footnote).foregroundStyle(.secondary)
                Text("Order \(order.orderId.prefix(8))").font(.caption2).monospaced().foregroundStyle(.secondary)
                if let paidAt = order.paidAt, let date = ISO8601DateFormatter().date(from: paidAt) {
                    Text(date.formatted(date: .abbreviated, time: .shortened)).font(.caption2).foregroundStyle(.secondary)
                }
                if let address = delivery.address {
                    Divider()
                    Label("Delivery preview", systemImage: "shippingbox").font(.footnote.weight(.semibold))
                    Text(address.label + "\n" + address.displayLines).font(.caption).foregroundStyle(.secondary).privacySensitive()
                    ForEach(Array(DemoDelivery.Stage.allCases.dropFirst().enumerated()), id: \.offset) { index, stage in
                        let done = DemoDelivery.Stage.allCases.firstIndex(of: delivery.stage)! >= index + 1
                        Label(stage.label, systemImage: done ? "checkmark.circle.fill" : "circle")
                            .font(.caption).foregroundStyle(done ? Theme.paid : .secondary)
                    }
                    if delivery.stage == .shipped || delivery.stage == .delivered {
                        Text("Demo tracking: SIM-\(order.orderId.prefix(8).uppercased())")
                            .font(.caption2).monospaced().foregroundStyle(.secondary)
                    }
                    if delivery.stage != .delivered {
                        Button("Next delivery update", action: onAdvanceDelivery)
                            .font(.footnote).frame(minHeight: 44)
                    }
                }
                Text("Preview only. No retailer order or shipment is created.")
                    .font(.caption2).foregroundStyle(.secondary)
                Button("Refresh receipt", action: onRefresh).font(.footnote).frame(minHeight: 44)
            }
        case .processing:
            Text("Checking PayPal's final result…").font(.footnote).foregroundStyle(.secondary)
            Button("Check status", action: onRefresh).font(.footnote).frame(minHeight: 44)
        case .failed(let message):
            Text(message).font(.footnote).foregroundStyle(.secondary)
            if approveOnWatch { approveButton(title: "Try again", action: onApprove) }
        case .working:
            HStack(spacing: 6) {
                ProgressView().frame(width: 18, height: 18)
                Text("Checking…").font(.caption2)
            }
        case .confirming(let prepared):
            // Amount and merchant above come from the signed payload, not the chat.
            Text(prepared.payload.merchantName)
                .font(.footnote).foregroundStyle(.secondary)
            Text("Sandbox payment · delivery preview").font(.caption2).foregroundStyle(.secondary)
            approveButton(title: "Confirm & pay", action: onConfirm)
            Button("Not now", role: .cancel, action: onCancel)
                .font(.footnote)
                .frame(minHeight: 44)
        case .awaiting:
            if approveOnWatch {
                Text("Sandbox payment · delivery preview").font(.caption2).foregroundStyle(.secondary)
                approveButton(title: "Review", action: onApprove).disabled(delivery.address == nil)
            } else {
                Text("Hold your device’s button to pay.").font(.caption2).foregroundStyle(.white.opacity(0.85))
            }
        }
    }

    private func approveButton(title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.footnote.weight(.semibold)).frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.small)
        .tint(Theme.accent)
        .padding(.top, 2)
        .accessibilityHint(order.isConfirming ? "Signs and submits this payment" : "Review the amount before paying")
    }
}

private struct MicButton: View {
    let busy: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: "mic.fill")
                Text("Talk")
            }
            .font(.footnote.weight(.semibold))
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .tint(Theme.accent)
        .controlSize(.small)
        .frame(minHeight: 44)
        .disabled(busy)
        .accessibilityLabel("Talk to ClawPay")
        .accessibilityHint("Sends your request after a short pause. Payments still need confirmation.")
    }
}

private struct SpeechInputView: View {
    let capture: SpeechCapture
    let onRetry: () -> Void
    let onCancel: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    switch capture.phase {
                    case .idle, .authorizing:
                        ProgressView("Microphone…")
                    case .recording:
                        Label("Listening…", systemImage: "mic.fill")
                            .font(.headline).foregroundStyle(.red)
                        TimelineView(.periodic(from: .now, by: 1)) { _ in
                            Text("\(capture.elapsed)s / 30s").font(.caption).monospacedDigit().foregroundStyle(.secondary)
                        }
                        Button("Stop & send", systemImage: "stop.fill") { capture.stop() }
                            .buttonStyle(.borderedProminent).tint(Theme.accent)
                    case .transcribing:
                        ProgressView("Transcribing…")
                    case .ready:
                        ProgressView("Sending…")
                    case .failed(let message):
                        Text(message).font(.footnote)
                        Button("Try again", action: onRetry)
                        typing(label: "Type instead")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .scenePadding(.horizontal)
                .padding(.vertical, 8)
            }
            .id(capture.phase)
            .navigationTitle("Voice")
            .containerBackground(Theme.background, for: .navigation)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: onCancel) }
            }
        }
    }

    private func typing(label: String) -> some View {
        TextFieldLink(prompt: Text("Your request")) { Text(label) } onSubmit: { capture.accept($0) }
    }
}

struct SettingsView: View {
    @Binding var baseURL: String
    @Binding var deviceToken: String
    @Binding var approveOnWatch: Bool
    let lastVoice: SpeechPlayer.Voice?
    let approverKey: ApproverKey?
    let keyError: String?

    var body: some View {
        Form {
            Section("Voice") {
                LabeledContent("Voice", value: "Fish Audio")
                if let lastVoice {
                    Text("Last reply: \(lastVoice.rawValue)").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Section {
                Toggle("Approve on watch", isOn: $approveOnWatch)
                if let key = approverKey {
                    LabeledContent("Key", value: key.kind.rawValue).font(.caption2)
                    LabeledContent("ID", value: key.fingerprint).font(.caption2.monospaced())
                    ShareLink(item: key.publicKeyPEM) { Label("Share public key", systemImage: "square.and.arrow.up") }
                } else if let keyError {
                    Text(keyError).font(.caption2).foregroundStyle(.red)
                }
            } header: {
                Text("Payments")
            } footer: {
                Text("Register this watch before enabling payment approvals.")
            }
            Section("Connection") {
                TextField("Server URL", text: $baseURL)
                    .textContentType(.URL)
            }
            Section("Delivery") {
                NavigationLink("Saved addresses") {
                    DeliveryAddressPicker(scope: SavedDeliveryAddresses.scope(token: deviceToken), manageSaved: true) { _ in }
                }.disabled(deviceToken.isEmpty)
                Text("Saved on this watch. Review before using for an order.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            Section("Device token") {
                SecureField("cpd_…", text: $deviceToken)
            }
        }
        .navigationTitle("Settings")
    }
}

#Preview {
    ContentView()
}

#if DEBUG && targetEnvironment(simulator)
private struct WatchReviewOptions: ViewModifier {
    func body(content: Content) -> some View {
        content
            .transformEnvironment(\.dynamicTypeSize) { size in
                if ProcessInfo.processInfo.environment["CLAWPAY_REVIEW_LARGE_TEXT"] == "1" { size = .xxxLarge }
            }
            .allowsHitTesting(ProcessInfo.processInfo.environment["CLAWPAY_REVIEW_SCREEN"] == nil)
    }
}

private enum WatchReviewScreen: String {
    case conversation, approval, confirmation, paid, processing, error, loading

    func apply(lines: inout [ChatLine], order: inout PendingOrder?, busy: inout Bool) {
        switch self {
        case .conversation:
            lines = [ChatLine(fromUser: true, text: "Find wool socks under $30."),
                     ChatLine(fromUser: false, text: "I found wool socks for USD 26.00 on Channel3. Would you like a pair?")]
        case .loading:
            lines = [ChatLine(fromUser: true, text: "Find wool socks.")]
            busy = true
        default:
            order = PendingOrder(orderId: "visual-review-only", total: "USD 26.00", merchant: "Channel3 Marketplace")
            order?.items = [AssistantReply.Item(name: "Insulated water bottle", qty: 1, lineTotal: "USD 26.00")]
            switch self {
            case .confirmation:
                order?.signedTotal = PreparedApproval.visualReview.payload.display
                order?.state = .confirming(.visualReview)
            case .paid: order?.state = .paid
            case .processing: order?.state = .processing
            case .error: order?.state = .failed("Couldn't reach the server. Check your connection and try again.")
            default: break
            }
        }
    }
}
#endif
