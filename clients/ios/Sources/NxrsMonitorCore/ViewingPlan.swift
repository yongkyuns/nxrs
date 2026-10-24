import Foundation

public enum ViewingError: Error { case invalidBudget, tooManyCameras }

/// Admission policy only. The native owner retains a slot until its cancelled
/// startup/remote cleanup has retired, even after closing the local decoder.
public struct ViewingPlan: Equatable, Sendable {
    public let start: [ResourceID]
    public let stop: [ResourceID]
    public init(requested: [ResourceID], active: Set<ResourceID>, retiring: Set<ResourceID>,
                blocked: Set<ResourceID> = [], reservedSlots: Int = 0, maximumActive: Int = 2) throws {
        guard (1...4).contains(maximumActive), (0...64).contains(reservedSlots) else { throw ViewingError.invalidBudget }
        guard requested.count <= 64 else { throw ViewingError.tooManyCameras }
        var seen = Set<ResourceID>()
        let wanted = requested.filter { !blocked.contains($0) && seen.insert($0).inserted }
            .prefix(maximumActive)
        let desired = Set(wanted)
        stop = active.subtracting(desired).sorted { $0.value < $1.value }
        let occupied = active.union(retiring)
        let capacity = max(0, maximumActive - occupied.count - reservedSlots)
        start = Array(wanted.filter { !occupied.contains($0) }.prefix(capacity))
    }
}
