// GameJamBench harness smoke — a trivially-passing C++ Spec test.
//
// Used by the spec_test validator to confirm:
//   - The blank_cpp_5_7_4 template's C++ Spec tests compile against UE 5.7.
//   - UE Automation framework discovers + runs them.
//   - `Automation RunTests GameJamBench.Harness.Smoke.*` writes a JSON report
//     the harness can parse.
//
// Tasks that need their own Spec tests should add files under
// `Source/BlankCpp/Tests/` with their task-id prefix
// (e.g. T020_AbilitySpec.cpp).

#include "Misc/AutomationTest.h"

#if WITH_DEV_AUTOMATION_TESTS

BEGIN_DEFINE_SPEC(FGameJamBenchHarnessSmokeSpec,
    "GameJamBench.Harness.Smoke",
    EAutomationTestFlags::EditorContext
    | EAutomationTestFlags::ProductFilter)
END_DEFINE_SPEC(FGameJamBenchHarnessSmokeSpec)

void FGameJamBenchHarnessSmokeSpec::Define()
{
    Describe("the spec_test validator path", [this]()
    {
        It("must run a trivially-passing assertion", [this]()
        {
            TestEqual(TEXT("1 + 1 should equal 2"), 1 + 1, 2);
        });

        It("must read true booleans as true", [this]()
        {
            TestTrue(TEXT("true is true"), true);
        });
    });
}

#endif // WITH_DEV_AUTOMATION_TESTS
