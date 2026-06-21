/**
 * Copyright (c) 2015-present, Facebook, Inc.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source code.
 */

#import <XCTest/XCTest.h>

#import <WebDriverAgentLib/FBDebugLogDelegateDecorator.h>
#import <WebDriverAgentLib/FBConfiguration.h>
#import <WebDriverAgentLib/FBFailureProofTestCase.h>
#import <WebDriverAgentLib/FBWebServer.h>
#import <WebDriverAgentLib/XCTestCase.h>

@interface UITestingUITests : FBFailureProofTestCase <FBWebServerDelegate>
@end

@implementation UITestingUITests

+ (void)setUp
{
  [FBDebugLogDelegateDecorator decorateXCTestLogger];
  [FBConfiguration disableRemoteQueryEvaluation];
  [FBConfiguration configureDefaultKeyboardPreferences];
  [FBConfiguration disableApplicationUIInterruptionsHandling];
  if (NSProcessInfo.processInfo.environment[@"ENABLE_AUTOMATIC_SCREEN_RECORDINGS"]) {
    [FBConfiguration enableScreenRecordings];
  } else {
    [FBConfiguration disableScreenRecordings];
  }
  if (NSProcessInfo.processInfo.environment[@"ENABLE_AUTOMATIC_SCREENSHOTS"]) {
    [FBConfiguration enableScreenshots];
  } else {
    [FBConfiguration disableScreenshots];
  }
  [super setUp];
}

/**
 Never ending test used to start WebDriverAgent
 */
- (void)testRunner
{
  // Idle keepalive timer.
  // iOS 26 retires (kills) an idle XCTest runner within ~5s when it is launched
  // without Xcode attached (e.g. driven directly via testmanagerd by
  // pymobiledevice3). A periodic NSLog on a background queue keeps the runner
  // process "hot" so it is not torn down while waiting for HTTP commands.
  // Inspired by callstack/agent-device PR #700.
  // Interval is tunable via the WDA_KEEPALIVE_INTERVAL env var (seconds).
  double keepaliveInterval = 3.0;
  NSString *envInterval = NSProcessInfo.processInfo.environment[@"WDA_KEEPALIVE_INTERVAL"];
  if (envInterval.length > 0) {
    double parsed = envInterval.doubleValue;
    if (parsed > 0) {
      keepaliveInterval = parsed;
    }
  }
  dispatch_queue_t keepaliveQueue = dispatch_queue_create("com.example.wda.keepalive", DISPATCH_QUEUE_SERIAL);
  dispatch_source_t keepaliveTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, keepaliveQueue);
  uint64_t intervalNs = (uint64_t)(keepaliveInterval * NSEC_PER_SEC);
  dispatch_source_set_timer(keepaliveTimer,
                            dispatch_time(DISPATCH_TIME_NOW, (int64_t)intervalNs),
                            intervalNs,
                            (uint64_t)(0.5 * NSEC_PER_SEC));
  dispatch_source_set_event_handler(keepaliveTimer, ^{
    NSLog(@"WDA_RUNNER_IDLE_KEEPALIVE");
  });
  dispatch_resume(keepaliveTimer);

  FBWebServer *webServer = [[FBWebServer alloc] init];
  webServer.delegate = self;
  [webServer startServing];

  // startServing blocks until shutdown; cancel the timer once it returns.
  if (keepaliveTimer) {
    dispatch_source_cancel(keepaliveTimer);
  }
}

#pragma mark - FBWebServerDelegate

- (void)webServerDidRequestShutdown:(FBWebServer *)webServer
{
  [webServer stopServing];
}

@end