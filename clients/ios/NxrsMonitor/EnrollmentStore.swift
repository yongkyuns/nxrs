import Foundation
import Security
import NxrsMonitorCore

/// One enrollment record keeps credentials and trusted origins together. Updating
/// origins never reuses an old token implicitly. No UserDefaults or iCloud sync.
struct Enrollment: Codable {
    let gateway: URL
    let mediaOrigin: URL?
    let token: String

    func policy() throws -> EndpointPolicy {
        _ = try BearerCredential(token)
        return try EndpointPolicy(gateway: gateway, additionalMediaOrigins: mediaOrigin.map { [$0] } ?? [])
    }
}

enum EnrollmentFailure: Error { case unavailable, invalid }

struct EnrollmentStore {
    private let service = "org.nxrs.monitor.enrollment.v1"
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: "gateway",
         kSecAttrSynchronizable as String: false]
    }

    func read() throws -> Enrollment? {
        var request = query
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data, data.count <= 16384 else {
            throw EnrollmentFailure.unavailable
        }
        guard let enrollment = try? JSONDecoder().decode(Enrollment.self, from: data),
              (try? enrollment.policy()) != nil else { throw EnrollmentFailure.invalid }
        return enrollment
    }

    func save(_ enrollment: Enrollment) throws {
        _ = try enrollment.policy()
        let data = try JSONEncoder().encode(enrollment)
        guard data.count <= 16384 else { throw EnrollmentFailure.invalid }
        let changes: [String: Any] = [kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let status = SecItemUpdate(query as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            changes.forEach { item[$0.key] = $0.value }
            guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else { throw EnrollmentFailure.unavailable }
        } else if status != errSecSuccess { throw EnrollmentFailure.unavailable }
    }

    func remove() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw EnrollmentFailure.unavailable }
    }
}
