return {
    LrSdkVersion = 15.0,
    LrSdkMinimumVersion = 14.3,
    LrToolkitIdentifier = "com.photo-ai-culler.lightroom-bridge",
    LrPluginName = "照片选片 · Lightroom 桥接",
    LrPluginInfoUrl = "https://developer.adobe.com/lightroom-classic/",
    LrInitPlugin = "Init.lua",
    LrShutdownPlugin = "Shutdown.lua",
    LrPluginInfoProvider = "PluginInfoProvider.lua",
    LrForceInitPlugin = true,
    LrLibraryMenuItems = {
        {
            title = "打开照片选片",
            file = "OpenTool.lua",
        },
    },
    VERSION = { major = 0, minor = 3, revision = 8, build = 0 },
}
