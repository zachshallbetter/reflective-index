import Foundation

protocol Greeter {
    func greet() -> String
}

struct Robin: Greeter {
    var name = "Robin"
    init(name: String) { self.name = name }
    init() {}
    func greet() -> String { helper() + name }
    private func helper() -> String { "Hi " }

    enum Mood { case calm, busy
        func label() -> String { self == .calm ? "calm" : "busy" }
    }
}

extension Robin {
    static func make() -> Robin { Robin() }
}

extension Robin {
    func wave() -> String { "👋" }
}

final class Alex {
    func run() -> String { Robin.make().greet() }
}

func topLevel() -> Int { 1 }
