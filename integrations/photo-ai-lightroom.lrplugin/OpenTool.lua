local LrHttp = import "LrHttp"
local LrTasks = import "LrTasks"

LrTasks.startAsyncTask(function()
    -- The desktop shell owns the dynamic loopback service URL.  Lightroom
    -- therefore talks only to the registered application protocol: Tauri will
    -- start the app when needed or focus the existing single instance.
    LrHttp.openUrlInBrowser("photoai://open")
end)
