import catalog from "./public-copy.json";

type ScenarioCopy = {
  name: string;
  summary: string;
  answer: string;
  decision: string;
};

type StateCopy = {
  heading: string;
  body: string;
};

export type CopyCatalog = {
  brand: Record<"name", string>;
  theme: Record<"label" | "dark" | "light", string>;
  gateway: Record<
    | "label"
    | "competition"
    | "demo"
    | "setup"
    | "actualRecord"
    | "noPublishedRecord"
    | "sample"
    | "paperSetup"
    | "competitionTitle"
    | "competitionIntro"
    | "setupTitle"
    | "setupIntro"
    | "ownerTitle"
    | "ownerBody"
    | "ownerAction"
    | "publicTitle"
    | "publicBody"
    | "selfHostTitle"
    | "selfHostBody"
    | "selfHostAction",
    string
  >;
  competitionRecord: Record<
    | "loading"
    | "unavailable"
    | "unavailableDetail"
    | "notPublished"
    | "notPublishedDetail"
    | "timeline"
    | "decision"
    | "noTrade"
    | "strategyNotReady"
    | "paperPosition"
    | "open"
    | "closed"
    | "spread"
    | "expiry"
    | "quantity"
    | "direction"
    | "bullish"
    | "bearish"
    | "neutral"
    | "entryFilled"
    | "rollFilled"
    | "closeFilled"
    | "holdReview"
    | "closeReview"
    | "rollReview"
    | "noActionReview"
    | "paperFill"
    | "positionReviewed"
    | "riskReduction"
    | "thesisChanged"
    | "positionAdjustment"
    | "dataIncomplete"
    | "recordId",
    string
  >;
  ownerSettings: Record<
    | "entry"
    | "title"
    | "close"
    | "checking"
    | "signInTitle"
    | "signInHelp"
    | "settingsCode"
    | "signIn"
    | "signingIn"
    | "signInFailed"
    | "sessionFailed"
    | "signedIn"
    | "sessionNote"
    | "signOut"
    | "providerTitle"
    | "providerHelp"
    | "provider"
    | "gemini"
    | "openAiCompatible"
    | "model"
    | "modelExampleGemini"
    | "modelExampleCompatible"
    | "endpoint"
    | "endpointHelp"
    | "providerFieldLabel"
    | "providerFieldHelp"
    | "save"
    | "saving"
    | "saved"
    | "saveFailed"
    | "currentTitle"
    | "notConfigured"
    | "configured"
    | "currentProvider"
    | "currentModel"
    | "currentEndpoint"
    | "generation"
    | "remove"
    | "removing"
    | "removed"
    | "removeFailed",
    string
  >;
  legal: Record<
    | "privacyLink"
    | "privacyTitle"
    | "importantLink"
    | "importantTitle"
    | "close"
    | "privacyReplayTitle"
    | "privacyReplayBody"
    | "privacyOwnerTitle"
    | "privacyOwnerBody"
    | "privacyKeyTitle"
    | "privacyKeyBody"
    | "privacyFlowTitle"
    | "privacyFlowBody"
    | "importantPaperTitle"
    | "importantPaperBody"
    | "importantAdviceTitle"
    | "importantAdviceBody"
    | "importantModelTitle"
    | "importantModelBody",
    string
  >;
  keyboardGuide: Record<
    | "label"
    | "title"
    | "help"
    | "close"
    | "nextScenario"
    | "previousScenario"
    | "previousTab"
    | "nextTab"
    | "toggleGuide"
    | "previousScenarioKey"
    | "nextScenarioKey"
    | "previousTabKey"
    | "nextTabKey"
    | "tabKey"
    | "moveFocus"
    | "questionKey"
    | "escapeKey",
    string
  >;
  provenance: Record<
    "banner" | "paperOnly" | "publicAccess" | "development" | "submission",
    string
  >;
  navigation: Record<"label" | "overview" | "comparison" | "run" | "record", string>;
  positionReview: Record<
    | "title"
    | "intro"
    | "helpLabel"
    | "helpSymbol"
    | "selectedPosition"
    | "spreadType"
    | "symbol"
    | "strikes"
    | "expiry"
    | "quantity"
    | "whatChanged"
    | "decision"
    | "thesisDrift"
    | "thesisDriftDefinition"
    | "tradePlan"
    | "positionNow"
    | "afterRoll"
    | "afterClose"
    | "ifHeld"
    | "noActionHeading"
    | "targetAtEntry"
    | "currentExposure"
    | "expectedExposure"
    | "brokerPosition"
    | "decisionRecord"
    | "orderStatus"
    | "noOrder"
    | "scenarioLabel"
    | "dteShort"
    | "dteLong"
    | "evidencePlan"
    | "evidenceCurrent"
    | "evidenceLastObserved"
    | "evidenceStatus"
    | "driftScore"
    | "planHorizon"
    | "timeRestored"
    | "timeTooShort"
    | "notCalculated"
    | "lastObserved"
    | "separator"
    | "oldSuffix"
    | "second"
    | "seconds"
    | "quoteAge"
    | "sampleAssessedAt"
    | "reviewBy"
    | "urgency"
    | "urgencyRoutine"
    | "urgencySoon"
    | "urgencyImmediate"
    | "urgencyWaiting"
    | "reviewWhenFresh"
    | "replacementTerms"
    | "replacementExpiry"
    | "replacementStrikes"
    | "replacementQuantity"
    | "estimatedRollCost"
    | "debitPerSpread"
    | "resultingMaxLoss"
    | "sampleEstimate",
    string
  >;
  acquisition: Record<
    | "title" | "event" | "eventValue" | "trigger" | "triggerValue"
    | "direction" | "directionValue" | "structure" | "structureValue"
    | "candidateCheck" | "candidateCheckValue" | "openingThesisValue"
    | "state" | "selected" | "rejected" | "noOp",
    string
  >;
  autonomy: Record<
    | "title" | "paper" | "armed" | "disarmed" | "scheduledTrigger"
    | "scheduledTriggerValue" | "emptyBookRoute" | "emptyBookRouteValue"
    | "managedPositionRoute" | "managedPositionRouteValue" | "policy" | "policyValue"
    | "reconcile" | "reconcileValue",
    string
  >;
  provenanceDetail: Record<
    | "title" | "source" | "sourceValue" | "observedAt" | "classification" | "support"
    | "supported" | "contradicted" | "unknown" | "supportingClassification"
    | "weakenedClassification" | "contradictedClassification" | "unknownClassification"
    | "sourceId" | "headline" | "eventCode" | "relation" | "materiality"
    | "relevance" | "confidence" | "sourceTier" | "primaryTierSample"
    | "originalReportingTierSample" | "secondaryTierSample" | "fixtureEvidenceNote"
    | "noClassification",
    string
  >;
  market: Record<
    | "title" | "mark" | "bidAsk" | "entryPrice" | "liquidationValue" | "openPnl"
    | "referenceSpot" | "iv" | "ivChange" | "dte" | "maxLoss" | "riskCap" | "orderState" | "noOrderState"
    | "staleValue" | "notCalculated",
    string
  >;
  tabs: Record<"label" | "evidence" | "choices" | "activity" | "record", string>;
  hero: Record<"eyebrow" | "title" | "body" | "chooseScenario" | "scenarioHelp", string>;
  performance: Record<
    | "label"
    | "sourceLabel"
    | "loading"
    | "notPublished"
    | "notPublishedDetail"
    | "unavailable"
    | "unavailableDetail"
    | "complete"
    | "missing"
    | "unknown"
    | "contaminated"
    | "baselineUnknown"
    | "baselineNotCaptured"
    | "completeDetail"
    | "missingDetail"
    | "unknownDetail"
    | "contaminatedDetail"
    | "baselineUnknownDetail"
    | "baselineNotCapturedDetail"
    | "captureStatus"
    | "captureComplete"
    | "captureMissing"
    | "captureUnknown"
    | "baselineStatus"
    | "baselineClean"
    | "baselineContaminated"
    | "baselineUnknownValue"
    | "baselineNotCapturedValue"
    | "startingEquity"
    | "startingEquityValue"
    | "currentEquity"
    | "equityChange"
    | "equityReturn"
    | "scheduledFor"
    | "attemptedAt"
    | "measuredAt"
    | "publishedAt"
    | "lifecycleCashflow"
    | "liquidationPnl"
    | "failureReason"
    | "failureCaptureNotStarted"
    | "failureProviderUnavailable"
    | "failureAccountIncomplete"
    | "failureBaselineUnavailable"
    | "failureSchemaInvalid"
    | "simulator"
    | "simulatorValue"
    | "simulatorDetail"
    | "publicationHash"
    | "predecessor"
    | "firstPublication"
    | "linkedPublication"
    | "linkedCertificates"
    | "notAvailable"
    | "notMeasured"
    | "currencySymbol"
    | "percentSymbol",
    string
  >;
  scenarios: Record<
    "THESIS_INTACT" | "THETA_TAKEOVER" | "CATALYST_BROKEN" | "STALE_QUOTE",
    ScenarioCopy
  >;
  decisionTrail: Record<"label" | "mismatch" | "action" | "alternatives" | "checked", string>;
  exposure: Record<
    | "title"
    | "intro"
    | "intended"
    | "intendedValue"
    | "measured"
    | "expected"
    | "reconciled"
    | "noBrokerOutcome"
    | "noBrokerOutcomeDetail",
    string
  >;
  thesis: Record<
    | "title"
    | "openingTitle"
    | "savedThesis"
    | "intendedDirection"
    | "timeCondition"
    | "timeConditionValue"
    | "invalidation"
    | "invalidationValue"
    | "sampleObservation"
    | "measuredExposure"
    | "whyItMatters"
    | "comparisonTitle"
    | "comparisonIntro"
    | "promise"
    | "measured"
    | "match"
    | "direction"
    | "volatility"
    | "horizon"
    | "risk"
    | "evidence"
    | "bullish"
    | "normalVolatility"
    | "fullHorizon"
    | "definedRisk"
    | "noContradiction"
    | "aligned"
    | "weakened"
    | "broken"
    | "outsidePlan"
    | "unknown"
    | "deltaHelp"
    | "thetaHelp"
    | "vegaHelp"
    | "dte"
    | "delta"
    | "theta"
    | "vega"
    | "maxLoss"
    | "dataAge",
    string
  >;
  drift: Record<
    | "title"
    | "direction"
    | "exposure"
    | "volatility"
    | "time"
    | "evidence"
    | "risk"
    | "score"
    | "scale"
    | "scoreScale"
    | "fresh"
    | "aging"
    | "stale"
    | "missing"
    | "crossed",
    string
  >;
  run: Record<
    | "title"
    | "intro"
    | "technologyTitle"
    | "tradingApi"
    | "mcp"
    | "model"
    | "cli"
    | "observe"
    | "validate"
    | "research"
    | "classify"
    | "measure"
    | "compare"
    | "enforce"
    | "certify"
    | "complete"
    | "notRun"
    | "liveRegion",
    string
  >;
  alternatives: Record<
    | "title"
    | "intro"
    | "blockedIntro"
    | "hold"
    | "close"
    | "roll"
    | "noAction"
    | "selected"
    | "rejected"
    | "unavailable"
    | "holdAligned"
    | "holdTheta"
    | "holdBroken"
    | "closeAligned"
    | "closeTheta"
    | "closeBroken"
    | "rollAligned"
    | "rollTheta"
    | "rollBroken"
    | "holdStale"
    | "closeStale"
    | "rollStale",
    string
  >;
  certificate: Record<
    | "title"
    | "intro"
    | "lifecycleTrail"
    | "originalPlan"
    | "whatChanged"
    | "choicesChecked"
    | "expectedResult"
    | "checkedResult"
    | "assessment"
    | "execution"
    | "action"
    | "terminal"
    | "disabled"
    | "before"
    | "beforeValue"
    | "expected"
    | "expectedHold"
    | "expectedClose"
    | "expectedRoll"
    | "expectedBlocked"
    | "checkedAfter"
    | "notApplicable"
    | "policyVersion"
    | "fixtureVersion"
    | "assessmentHash"
    | "inputHash"
    | "howChecked"
    | "details"
    | "limitations",
    string
  >;
  states: {
    title: string;
    cold: StateCopy;
    noPosition: StateCopy;
    stale: StateCopy;
    unknown: StateCopy;
    assignment: StateCopy;
    blocked: StateCopy;
  };
  footer: Record<"api" | "keyboard", string>;
};

export const copy = catalog as CopyCatalog;
