public struct Jordan {
    public init() {}
    public func plan() -> Int { step() + Leo.count() }
    func step() -> Int { helper(2) }
    static func note() -> Int { 1 }
    func viaSelf() -> Int { Self.note() }
    func pick() -> (Int) -> Int { helper }
    var total: Int {
        helper(3) + Leo.count()
    }
}

public enum Leo {
    public static func count() -> Int { [1, 2, 3].count }
}

func helper(_ n: Int) -> Int { n * 2 }
