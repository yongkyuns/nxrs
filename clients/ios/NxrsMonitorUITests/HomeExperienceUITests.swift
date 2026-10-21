import XCTest

final class HomeExperienceUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
    }

    private func launch(_ arguments: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = arguments
        app.launch()
        return app
    }

    private func screenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testOnboardingAndInsecureAddressValidation() {
        let app = launch(["--onboarding-preview"])
        XCTAssertTrue(app.buttons["connect-home"].waitForExistence(timeout: 10))
        screenshot("Welcome")
        app.buttons["connect-home"].tap()
        let address = app.textFields["gateway-address"]
        XCTAssertTrue(address.waitForExistence(timeout: 3))
        address.tap()
        address.typeText("http://home.example")
        let key = app.secureTextFields["viewer-key"]
        key.tap()
        key.typeText("ui-test-key")
        app.swipeUp()
        app.buttons["save-home"].tap()
        XCTAssertTrue(app.staticTexts["Enter a valid HTTPS home address and viewer key, or import an invitation."].waitForExistence(timeout: 3))
        screenshot("Connection validation")
    }

    func testCameraPersonalizationNavigationAndLandscape() {
        let app = launch(["--design-preview"])
        XCTAssertTrue(app.staticTexts["design-preview-banner"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Snapshot"].isEnabled)
        screenshot("Home light")
        app.buttons["Camera settings"].tap()
        XCTAssertTrue(app.textFields["camera-name"].waitForExistence(timeout: 3))
        app.buttons["Clear camera name"].tap()
        app.textFields["camera-name"].tap()
        app.textFields["camera-name"].typeText("Reading nook")
        app.buttons["save-camera-settings"].tap()
        XCTAssertTrue(app.staticTexts["Reading nook"].firstMatch.waitForExistence(timeout: 3))
        app.buttons["expand-camera"].tap()
        XCTAssertTrue(app.buttons["Close full screen"].waitForExistence(timeout: 3))
        screenshot("Full screen portrait")
        XCUIDevice.shared.orientation = .landscapeLeft
        XCTAssertTrue(app.buttons["Record clip"].waitForExistence(timeout: 3))
        screenshot("Full screen landscape")
        app.buttons["Close full screen"].tap()
        XCUIDevice.shared.orientation = .portrait
        app.tabBars.buttons["Recordings"].tap()
        XCTAssertTrue(app.staticTexts["No recordings yet"].waitForExistence(timeout: 3))
        screenshot("Recordings empty state")
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.staticTexts["Nxrs Home"].waitForExistence(timeout: 3))
        app.buttons["appearance-picker"].tap()
        app.buttons["Dark"].tap()
        app.tabBars.buttons["Home"].tap()
        screenshot("Home dark")
    }

    func testAccessibilityTextAndCameraListScrolling() {
        let app = launch(["--design-preview", "--preview-dark",
                          "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"])
        XCTAssertTrue(app.staticTexts["design-preview-banner"].waitForExistence(timeout: 10))
        screenshot("Home accessibility text")
        for _ in 0..<4 { app.swipeUp() }
        XCTAssertTrue(app.buttons["Edit Garden"].exists)
        screenshot("Camera list accessibility text")
        app.tabBars.buttons["Recordings"].tap()
        XCTAssertTrue(app.staticTexts["No recordings yet"].waitForExistence(timeout: 3))
        screenshot("Recordings accessibility text")
    }

    func testSavedRecordingFavoriteShareAndDeletion() {
        let app = launch(["--design-preview", "--preview-library"])
        app.tabBars.buttons["Recordings"].tap()
        let recording = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Snapshot from UI test fixture")).firstMatch
        XCTAssertTrue(recording.waitForExistence(timeout: 10))
        screenshot("Saved recordings")
        recording.tap()
        app.buttons["Favorite"].tap()
        XCTAssertTrue(app.buttons["Favorited"].waitForExistence(timeout: 3))
        screenshot("Saved recording detail")
        app.buttons["Share"].tap()
        screenshot("System share sheet")
        let sharing = app.otherElements["ShareSheet.RemoteContainerView"]
        XCTAssertTrue(sharing.waitForExistence(timeout: 5))
        let close = sharing.buttons["Close"].firstMatch
        // The remote container appears before its native header on older iOS.
        if close.waitForExistence(timeout: 5) { close.tap() }
        else {
            // Newer iOS uses a bottom popover. Its dismiss region can extend far
            // outside the screen, so tap the visible window above the popover.
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.15)).tap()
        }
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: sharing)
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
        app.buttons["Delete recording"].tap()
        app.buttons["Delete"].tap()
        XCTAssertTrue(app.staticTexts["No recordings yet"].waitForExistence(timeout: 5))
    }

    func testCameraFavoritesAndRoomFilters() {
        let app = launch(["--design-preview"])
        XCTAssertTrue(app.buttons["Remove camera favorite"].waitForExistence(timeout: 10))
        app.buttons["Remove camera favorite"].tap()
        app.swipeUp()
        app.buttons["Favorites"].tap()
        XCTAssertTrue(app.staticTexts["Nothing here just yet"].waitForExistence(timeout: 3))
        app.buttons["All cameras"].tap()
        XCTAssertTrue(app.buttons["Edit Front door"].waitForExistence(timeout: 3))
        app.scrollViews["camera-filters"].swipeLeft()
        app.buttons["Entrance"].tap()
        let selected = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isSelected == true"), object: app.buttons["Entrance"])
        XCTAssertEqual(XCTWaiter.wait(for: [selected], timeout: 3), .completed)
        XCTAssertTrue(app.buttons["Edit Front door"].waitForExistence(timeout: 3))
        for camera in ["Edit Living room", "Edit Garden"] {
            let removed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons[camera])
            XCTAssertEqual(XCTWaiter.wait(for: [removed], timeout: 3), .completed)
        }
        screenshot("Camera room filter")
        app.buttons["Edit Front door"].tap()
        app.buttons["Clear room name"].tap()
        app.textFields["camera-room"].tap()
        app.textFields["camera-room"].typeText("Kitchen")
        app.buttons["save-camera-settings"].tap()
        XCTAssertTrue(app.buttons["All cameras"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["All cameras"].isSelected,
                      "Removing the active room must not leave an invisible stale filter")
        XCTAssertTrue(app.buttons["Edit Living room"].exists)
    }

    func testSavedRecordingAtLargestTextSize() {
        let app = launch(["--design-preview", "--preview-library",
                          "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"])
        app.tabBars.buttons["Recordings"].tap()
        let recording = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Snapshot from UI test fixture")).firstMatch
        XCTAssertTrue(recording.waitForExistence(timeout: 10))
        recording.tap()
        XCTAssertTrue(app.buttons["Delete recording"].waitForExistence(timeout: 3))
        screenshot("Saved recording accessibility text")
        if !app.buttons["Favorite"].isHittable { app.swipeUp() }
        XCTAssertTrue(app.buttons["Favorite"].isHittable)
        app.buttons["Favorite"].tap()
        XCTAssertTrue(app.buttons["Favorited"].waitForExistence(timeout: 3))
        if !app.buttons["Delete recording"].isHittable { app.swipeUp() }
        XCTAssertTrue(app.buttons["Share"].isHittable)
        XCTAssertTrue(app.buttons["Delete recording"].isHittable)
        screenshot("Saved recording accessibility actions")
        app.buttons["Done"].tap()
    }
}
