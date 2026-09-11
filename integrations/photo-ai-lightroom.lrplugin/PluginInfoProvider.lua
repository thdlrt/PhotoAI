local LrView = import "LrView"

return {
    sectionsForBottomOfDialog = function(f, propertyTable)
        local bridge = _G.PhotoAiLightroomBridge
        local root = bridge and bridge.root or "尚未配置；请先由照片选片生成 bridge-path.txt"
        return {
            {
                title = "本地队列",
                f:column {
                    spacing = f:control_spacing(),
                    f:static_text {
                        title = root,
                        width_in_chars = 70,
                    },
                    f:static_text {
                        title = "Lightroom 必须保持打开。预览使用并删除隔离虚拟副本；正式保存才建立快照并写入 XMP。",
                        width_in_chars = 70,
                    },
                },
            },
        }
    end,
}
