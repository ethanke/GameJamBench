using UnrealBuildTool;

public class BlankCppTarget : TargetRules
{
    public BlankCppTarget(TargetInfo Target) : base(Target)
    {
        Type = TargetType.Game;
        DefaultBuildSettings = BuildSettingsVersion.Latest;
        IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
        ExtraModuleNames.Add("BlankCpp");
    }
}
