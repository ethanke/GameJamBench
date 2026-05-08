using UnrealBuildTool;

public class BlankCppEditorTarget : TargetRules
{
    public BlankCppEditorTarget(TargetInfo Target) : base(Target)
    {
        Type = TargetType.Editor;
        DefaultBuildSettings = BuildSettingsVersion.Latest;
        IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
        ExtraModuleNames.Add("BlankCpp");
    }
}
