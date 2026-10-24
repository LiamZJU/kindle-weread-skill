local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local lfs = require("libs/libkoreader-lfs")

local LOGIN_TEXT = table.concat({
    "请用微信扫描接下来的二维码，登录微信读书。",
    "",
    "扫之前，先在手机微信读书里点：",
    "我 → 设置 → 微信读书 Skill → 获取 API Key。",
    "",
    "Kindle 需要连着 Wi-Fi。",
}, "\n")

local WeKindle = WidgetContainer:extend{
    name = "wekindle",
    is_doc_only = false,
}

function WeKindle:simpleuiEnabled()
    local main = DataStorage:getDataDir() .. "/plugins/simpleui.koplugin/main.lua"
    if lfs.attributes(main, "mode") ~= "file" then
        return false
    end
    local disabled = G_reader_settings:readSetting("plugins_disabled") or {}
    return not disabled.simpleui and not disabled["simpleui.koplugin"]
end

function WeKindle:homeRequested()
    local marker = DataStorage:getDataDir() .. "/wekindle_home"
    return lfs.attributes(marker, "mode") == "file"
end

-- A wireless install must not edit settings.reader.lua while KOReader is running:
-- exit would write the in-memory copy back over it. Old installs left autostart
-- on. Clear it on the next launch, even when the home screen was already set.
function WeKindle:disableSshAutostart()
    if G_reader_settings:readSetting("SSH_autostart") == false then
        return
    end
    G_reader_settings:saveSetting("SSH_autostart", false)
    if G_reader_settings.flush then
        G_reader_settings:flush()
    end
end

-- SimpleUI installs its launch patch during its own init. This runs in the
-- same startup, before FileManager builds the first screen.
function WeKindle:ensureHome()
    if G_reader_settings:readSetting("wekindle_start_with_set") == true then
        return
    end
    if not self:homeRequested() or not self:simpleuiEnabled() then
        return
    end
    G_reader_settings:saveSetting("start_with", "homescreen_simpleui")
    G_reader_settings:saveSetting("wekindle_start_with_set", true)
    if G_reader_settings.flush then
        G_reader_settings:flush()
    end
end

function WeKindle:wereadInstance()
    if self.ui and self.ui.weread then
        return self.ui.weread
    end
    local filemanager = package.loaded["apps/filemanager/filemanager"]
    local instance = filemanager and filemanager.instance
    if instance and instance.weread then
        return instance.weread
    end
    return nil
end

function WeKindle:offerLogin(attempt)
    if self._login_offered then
        return
    end
    local weread = self:wereadInstance()
    if not weread or not weread.qr_login or not weread.settings then
        if attempt < 3 then
            UIManager:scheduleIn(1, function()
                self:offerLogin(attempt + 1)
            end)
        end
        return
    end
    if weread.settings.is_api_configured and weread.settings:is_api_configured() then
        return
    end
    if weread.qr_login.qr_dialog then
        return
    end
    self._login_offered = true
    UIManager:show(ConfirmBox:new{
        text = LOGIN_TEXT,
        ok_text = "显示二维码",
        cancel_text = "稍后",
        ok_callback = function()
            weread.qr_login:start()
        end,
    })
end

function WeKindle:init()
    self:disableSshAutostart()
    self:ensureHome()
    if self.ui and self.ui.document then
        return
    end
    UIManager:scheduleIn(1.5, function()
        self:offerLogin(1)
    end)
end

return WeKindle
