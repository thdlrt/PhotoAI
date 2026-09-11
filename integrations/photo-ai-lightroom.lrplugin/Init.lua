-- Lightroom re-executes this init script when a plug-in is reloaded, but Lua's
-- require cache can retain the previous Bridge module. Clear only our module so
-- disabling and enabling the plug-in reliably picks up source updates.
if package and package.loaded then
    package.loaded["Bridge"] = nil
end

local Bridge = require "Bridge"

_G.PhotoAiLightroomBridge = Bridge.new(_PLUGIN.path)
_G.PhotoAiLightroomBridge:start()
