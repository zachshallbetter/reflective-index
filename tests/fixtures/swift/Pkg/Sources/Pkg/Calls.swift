public struct Jordan {
    public init() {}
    public func plan() -> Int { step() + Leo.count() }
    func step() -> Int { helper(2) }
}

public enum Leo {
    public static func count() -> Int { [1, 2, 3].count }
}

func helper(_ n: Int) -> Int { n * 2 }
