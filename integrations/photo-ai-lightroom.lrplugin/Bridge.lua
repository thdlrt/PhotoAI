local LrApplication = import "LrApplication"
local LrApplicationView = import "LrApplicationView"
local LrDigest = import "LrDigest"
local LrExportSession = import "LrExportSession"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrSelection = import "LrSelection"
local LrTasks = import "LrTasks"

local Bridge = {}
Bridge.__index = Bridge

local TASK_PROTOCOL = "PHOTO_AI_LR_TASK/1"
local RESULT_PROTOCOL = "PHOTO_AI_LR_RESULT/1"
local HEARTBEAT_PROTOCOL = "PHOTO_AI_LR_HEARTBEAT/1"
local XMP_BACKUP_PROTOCOL = "PHOTO_AI_LR_XMP_BACKUP/1"
local PRESET_PROTOCOL = "PHOTO_AI_LR_PRESET/1"
local LOOK_DESCRIPTOR_PROTOCOL = "PHOTO_AI_LR_LOOK/1"
local LOOK_DESCRIPTOR_ENTRY_PROTOCOL = "PHOTO_AI_LR_LOOK_ENTRY/1"
local PREVIEW_COPY_PROTOCOL = "PHOTO_AI_LR_PREVIEW_COPY/1"
local PLUGIN_VERSION = "0.3.8"
local SNAPSHOT_PREFIX = "照片选片 · 处理前"
local PREVIEW_COPY_PREFIX = "照片选片 · 隔离预览"
local CANCEL_ERROR = "__PHOTO_AI_LIGHTROOM_CANCELLED__"
local SIDECAR_POLL_SECONDS = 0.25
local SIDECAR_STABLE_POLLS = 3
-- Lightroom Classic 15 can return from photo:saveMetadata() long before its
-- background XMP queue reaches a newly imported photo. Keep polling the actual
-- sidecar and periodically renew the save request instead of treating the old
-- 25-second window as a hard failure.
local SIDECAR_TIMEOUT_POLLS = 4800
local SIDECAR_SAVE_RETRY_POLLS = 80
local MAX_XMP_BACKUP_BYTES = 16 * 1024 * 1024
local MAX_MANAGED_REGISTRY_BYTES = 8 * 1024 * 1024
local MAX_MANAGED_PRESETS = 5000
local MAX_LOOK_DESCRIPTOR_BYTES = 4 * 1024 * 1024
local MAX_LOOK_DESCRIPTOR_NODES = 10000
local MAX_LOOK_DESCRIPTOR_DEPTH = 16
local MAX_PREVIEW_SELECTION = 5000
local MANAGED_REGISTRY_FILE = "managed-preset-registry.lua"
local MANAGED_REGISTRATION_FILE = "managed-preset-registration.json"
local SIDECAR_RAW = {
    arw = true, cr2 = true, cr3 = true, nef = true, nrw = true,
    raf = true, orf = true, rw2 = true, pef = true,
}

local function read_first_line(path)
    local handle = io.open(path, "rb")
    if not handle then return nil end
    local line = handle:read("*l")
    handle:close()
    return line
end

local function read_bridge_pointer(path)
    local handle = io.open(path, "rb")
    if not handle then return nil, nil end
    local root = handle:read("*l")
    local style_root = handle:read("*l")
    handle:close()
    return root, style_root
end

local function read_all(path, maximum_bytes)
    local attributes = LrFileUtils.fileAttributes(path)
    local size = attributes and tonumber(attributes.fileSize)
    if not size then error("cannot measure file: " .. path) end
    if maximum_bytes and size > maximum_bytes then
        error("file is too large for safe bridge processing: " .. tostring(size))
    end
    local contents = LrFileUtils.readFile(path)
    if not contents or #contents ~= size then error("short read while processing: " .. path) end
    return contents
end

local function percent_decode(value)
    return (value:gsub("%%(%x%x)", function(hex)
        return string.char(tonumber(hex, 16))
    end))
end

local function percent_encode(value)
    return (tostring(value):gsub("([^A-Za-z0-9_.~-])", function(character)
        return string.format("%%%02X", string.byte(character))
    end))
end

local function decode_scalar(value)
    local decoded = percent_decode(value)
    local kind = decoded:sub(1, 1)
    if decoded:sub(2, 2) ~= "|" then error("invalid typed field") end
    local payload = decoded:sub(3)
    if kind == "b" then
        if payload == "1" then return true end
        if payload == "0" then return false end
        error("invalid boolean field")
    elseif kind == "i" or kind == "f" then
        local number = tonumber(payload)
        if not number or number ~= number or number == math.huge or number == -math.huge then
            error("invalid numeric field")
        end
        return number
    elseif kind == "s" then
        return payload
    end
    error("unknown typed field")
end

local function encode_scalar(value)
    if type(value) == "boolean" then
        return percent_encode(value and "b|1" or "b|0")
    elseif type(value) == "number" then
        return percent_encode("f|" .. tostring(value))
    end
    return percent_encode("s|" .. tostring(value))
end

local function parse_line(line, expected_protocol)
    if not line then error("empty task") end
    local fields = {}
    local first = true
    for part in (line .. "\t"):gmatch("(.-)\t") do
        if first then
            if part ~= expected_protocol then error("unsupported protocol") end
            first = false
        else
            local separator = part:find("=", 1, true)
            if not separator then error("invalid field") end
            local key = part:sub(1, separator - 1)
            if key == "" or fields[key] ~= nil then error("invalid or duplicate field") end
            fields[key] = decode_scalar(part:sub(separator + 1))
        end
    end
    return fields
end

local function result_line(fields)
    local ordered = {
        "batch_id", "task_id", "photo_path", "status", "finished_at", "message",
        "task_type", "output_mode", "xmp_status", "xmp_path", "jpeg_status",
        "jpeg_path", "restore_status", "isolation_kind", "isolation_status",
        "source_uuid", "working_uuid", "cleanup_count", "preset_status", "preset_uuid",
        "preset_scope", "preset_amount", "preset_count", "preset_list_path",
        "look_status", "look_uuid", "look_amount",
    }
    local parts = { RESULT_PROTOCOL }
    for _, key in ipairs(ordered) do
        if fields[key] ~= nil then
            table.insert(parts, key .. "=" .. encode_scalar(fields[key]))
        end
    end
    return table.concat(parts, "\t") .. "\n"
end

local function delete_if_exists(path)
    if not LrFileUtils.exists(path) then return end
    local deleted, delete_error = LrFileUtils.delete(path)
    if not deleted then error(delete_error or ("cannot delete file: " .. path)) end
end

local function atomic_write(path, contents)
    local temporary = path .. ".tmp"
    local handle, open_error = io.open(temporary, "wb")
    if not handle then error(open_error or "cannot create temporary file") end
    handle:write(contents)
    handle:flush()
    handle:close()
    delete_if_exists(path)
    local moved, move_error = LrFileUtils.move(temporary, path)
    if not moved then
        pcall(function() delete_if_exists(temporary) end)
        error(move_error or "cannot publish file")
    end
end

local function atomic_create(path, contents)
    if LrFileUtils.exists(path) then error("refusing to overwrite safety record: " .. path) end
    local temporary = path .. ".tmp"
    delete_if_exists(temporary)
    local handle, open_error = io.open(temporary, "wb")
    if not handle then error(open_error or "cannot create temporary safety record") end
    handle:write(contents)
    handle:flush()
    handle:close()
    if LrFileUtils.exists(path) then
        delete_if_exists(temporary)
        error("safety record appeared concurrently: " .. path)
    end
    local moved, move_error = LrFileUtils.move(temporary, path)
    if not moved then
        pcall(function() delete_if_exists(temporary) end)
        error(move_error or "cannot publish safety record")
    end
end

local function now_utc()
    return os.date("!%Y-%m-%dT%H:%M:%SZ")
end

local function json_string(value)
    local bytes = { '"' }
    local text = tostring(value or "")
    for index = 1, #text do
        local byte = text:byte(index)
        if byte == 34 then
            table.insert(bytes, '\\"')
        elseif byte == 92 then
            table.insert(bytes, "\\\\")
        elseif byte == 8 then
            table.insert(bytes, "\\b")
        elseif byte == 9 then
            table.insert(bytes, "\\t")
        elseif byte == 10 then
            table.insert(bytes, "\\n")
        elseif byte == 12 then
            table.insert(bytes, "\\f")
        elseif byte == 13 then
            table.insert(bytes, "\\r")
        elseif byte < 32 then
            table.insert(bytes, string.format("\\u%04x", byte))
        else
            table.insert(bytes, string.char(byte))
        end
    end
    table.insert(bytes, '"')
    return table.concat(bytes)
end

local function safe_message(value)
    local message = tostring(value or "unknown error")
    message = message:gsub("[\r\n\t]", " ")
    return message:sub(1, 1000)
end

local function task_name(path)
    return LrPathUtils.leafName(path):gsub("%.task$", "")
end

local function has_items(table_value)
    return next(table_value) ~= nil
end

local function raw_extension(path)
    return ((LrPathUtils.extension(path) or ""):gsub("^%.", "")):lower()
end

local function valid_identifier(value)
    return type(value) == "string"
        and #value >= 1
        and #value <= 96
        and value:match("^[A-Za-z0-9][A-Za-z0-9._-]*$") ~= nil
end

local function path_key(path)
    return tostring(path or ""):gsub("/", "\\"):lower()
end

local function has_relative_segment(path)
    for segment in tostring(path or ""):gmatch("[^/\\]+") do
        if segment == "." or segment == ".." then return true end
    end
    return false
end

local function absolute_windows_path(path)
    return type(path) == "string" and (
        path:match("^%a:[/\\]") ~= nil or path:match("^[/\\][/\\]") ~= nil
    )
end

local function valid_sha256(value)
    return type(value) == "string" and #value == 64 and value:match("^%x+$") ~= nil
end

local function look_path_segments(path)
    if type(path) ~= "string" or path == "" or #path > 1400
        or path:sub(1, 1) == "/" or path:sub(-1) == "/" or path:find("//", 1, true) then
        error("invalid Look descriptor path")
    end
    local segments = {}
    for segment in path:gmatch("[^/]+") do
        if #segment > 80 or not segment:match("^[A-Za-z0-9_.-]+$")
            or segment == "." or segment == ".." then
            error("invalid Look descriptor path segment")
        end
        if segment:match("^[0-9]+$") and not segment:match("^[1-9][0-9]*$") then
            error("Look descriptor numeric paths must be positive canonical indices")
        end
        local key = segment
        if segment:match("^[1-9][0-9]*$") then key = tonumber(segment) end
        table.insert(segments, key)
    end
    if #segments < 1 or #segments > MAX_LOOK_DESCRIPTOR_DEPTH then
        error("Look descriptor nesting is outside the safety limit")
    end
    return segments
end

local LOOK_ROOT_FIELDS = {
    Amount = true,
    Cluster = true,
    Group = true,
    Name = true,
    Parameters = true,
    SupportsAmount = true,
    UUID = true,
}

local function parse_look_descriptor(contents, expected_uuid)
    local lines = {}
    for line in tostring(contents or ""):gmatch("[^\r\n]+") do table.insert(lines, line) end
    if #lines < 2 then error("Look descriptor is empty") end
    local header = parse_line(lines[1], LOOK_DESCRIPTOR_PROTOCOL)
    for key in pairs(header) do
        if key ~= "uuid" and key ~= "entry_count" then
            error("unsupported Look descriptor header field: " .. tostring(key))
        end
    end
    if not valid_identifier(header.uuid) or header.uuid ~= expected_uuid then
        error("Look descriptor UUID does not match the task")
    end
    if type(header.entry_count) ~= "number" or header.entry_count < 1
        or header.entry_count > MAX_LOOK_DESCRIPTOR_NODES
        or header.entry_count ~= math.floor(header.entry_count)
        or #lines ~= header.entry_count + 1 then
        error("Look descriptor entry_count does not match its records")
    end

    local root = {}
    local seen = {}
    for index = 2, #lines do
        local fields = parse_line(lines[index], LOOK_DESCRIPTOR_ENTRY_PROTOCOL)
        for key in pairs(fields) do
            if key ~= "path" and key ~= "kind" and key ~= "value" then
                error("unsupported Look descriptor entry field: " .. tostring(key))
            end
        end
        if type(fields.path) ~= "string" or seen[fields.path] then
            error("Look descriptor paths must be unique strings")
        end
        seen[fields.path] = true
        local segments = look_path_segments(fields.path)
        if not LOOK_ROOT_FIELDS[segments[1]] then
            error("unsupported Look root field: " .. tostring(segments[1]))
        end
        local parent = root
        for segment_index = 1, #segments - 1 do
            local child = parent[segments[segment_index]]
            if type(child) ~= "table" then
                error("Look descriptor table parents must precede their children")
            end
            parent = child
        end
        local leaf = segments[#segments]
        if parent[leaf] ~= nil then error("Look descriptor path collision") end
        if fields.kind == "table" and fields.value == nil then
            parent[leaf] = {}
        elseif fields.kind == "value" and fields.value ~= nil then
            parent[leaf] = fields.value
        else
            error("invalid Look descriptor entry kind/value shape")
        end
    end
    if root.UUID ~= expected_uuid then error("Look descriptor body UUID does not match the task") end
    if type(root.Amount) ~= "number" or root.Amount < 0 or root.Amount > 2 then
        error("Look descriptor Amount must be a factor from 0 to 2")
    end
    if type(root.Parameters) ~= "table" then error("Look descriptor Parameters must be a table") end
    return root
end

local function same_photo(left, right)
    if left == right then return true end
    if not left or not right then return false end
    local left_id = left.localIdentifier
    local right_id = right.localIdentifier
    return left_id ~= nil and right_id ~= nil and left_id == right_id
end

local function sidecar_signature(path)
    if not LrFileUtils.exists(path) then
        return { exists = false, size = nil, modified = nil }
    end
    local attributes = LrFileUtils.fileAttributes(path)
    if type(attributes) ~= "table" then error("cannot read XMP file attributes: " .. path) end
    local size = tonumber(attributes.fileSize)
    local modified = attributes.fileModificationDate
    if size == nil or modified == nil then error("XMP file attributes are incomplete: " .. path) end
    return { exists = true, size = size, modified = tostring(modified) }
end

local function signature_key(signature)
    if not signature.exists then return "missing" end
    return tostring(signature.size) .. "@" .. tostring(signature.modified)
end

local function xmp_value(contents, qualified_name)
    local escaped = qualified_name:gsub("(%W)", "%%%1")
    return contents:match(escaped .. '%s*=%s*"([^"]*)"')
        or contents:match(escaped .. "%s*=%s*'([^']*)'")
        or contents:match("<" .. escaped .. ">%s*([^<]-)%s*</" .. escaped .. ">")
end

local function require_xmp_number(contents, qualified_name, expected, tolerance)
    local actual = tonumber(xmp_value(contents, qualified_name))
    if not actual or math.abs(actual - expected) > (tolerance or 0) then
        error("XMP verification failed for " .. qualified_name
            .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
    end
end

local function require_xmp_string(contents, qualified_name, expected)
    local actual = xmp_value(contents, qualified_name)
    if actual ~= expected then
        error("XMP verification failed for " .. qualified_name
            .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
    end
end

local function require_xmp_profile_amount(contents, expected)
    local actual = tonumber(xmp_value(contents, "crs:ProfileAmount"))
    -- The Lightroom SDK exposes profile Amount as 0.0..2.0 while Camera Raw
    -- serializes the same value as either that factor or 0..200 depending on
    -- process version.  Accept both encodings, but never accept a missing or
    -- otherwise different value.
    if not actual or (
        math.abs(actual - expected) > 0.0001
        and math.abs(actual - (expected * 100)) > 0.01
    ) then
        error("XMP verification failed for crs:ProfileAmount"
            .. ": expected " .. tostring(expected) .. " (or "
            .. tostring(expected * 100) .. "), got " .. tostring(actual))
    end
end

local function collect_task_entries(directory)
    local entries = {}
    for path in LrFileUtils.directoryEntries(directory) do
        if path:match("%.task$") then table.insert(entries, path) end
    end
    table.sort(entries)
    return entries
end

local function collect_suffix_entries(directory, suffix)
    local entries = {}
    for path in LrFileUtils.directoryEntries(directory) do
        local leaf = LrPathUtils.leafName(path)
        if leaf:sub(-#suffix) == suffix then table.insert(entries, path) end
    end
    table.sort(entries)
    return entries
end

local function preview_copy_name(batch_id, task_id)
    return PREVIEW_COPY_PREFIX .. " · " .. batch_id .. "--" .. task_id
end

local function opaque_identity(value)
    return type(value) == "string"
        and #value >= 1
        and #value <= 200
        and value:find("[%c]") == nil
end

local function uuid_identity(value)
    return opaque_identity(value) and value:match("^[A-Za-z0-9{}._-]+$") ~= nil
end

local function preview_copy_record_line(record)
    local ordered = {
        "phase", "batch_id", "task_id", "catalog_path", "photo_path",
        "source_uuid", "source_local_id", "working_uuid", "working_local_id",
        "selection_active_uuid", "selection_other_uuids", "copy_name",
        "creation_started", "created_at", "removed_at",
    }
    local parts = { PREVIEW_COPY_PROTOCOL }
    for _, key in ipairs(ordered) do
        if record[key] ~= nil then
            table.insert(parts, key .. "=" .. encode_scalar(record[key]))
        end
    end
    return table.concat(parts, "\t") .. "\n"
end

local function file_signature(path)
    if not LrFileUtils.exists(path) then return nil end
    local attributes = LrFileUtils.fileAttributes(path)
    if type(attributes) ~= "table" then error("cannot inspect managed preset registry") end
    local size = tonumber(attributes.fileSize)
    local modified = attributes.fileModificationDate
    if not size or modified == nil then error("managed preset registry attributes are incomplete") end
    return tostring(size) .. "@" .. tostring(modified)
end

local function validate_develop_value(value, state, depth)
    if depth > 12 then error("managed preset develop settings are nested too deeply") end
    state.nodes = state.nodes + 1
    if state.nodes > 200000 then error("managed preset develop settings are too large") end
    local value_type = type(value)
    if value_type == "nil" or value_type == "boolean" then return end
    if value_type == "number" then
        if value ~= value or value == math.huge or value == -math.huge then
            error("managed preset contains a non-finite number")
        end
        return
    end
    if value_type == "string" then
        if #value > 1024 * 1024 then error("managed preset contains an oversized string") end
        return
    end
    if value_type ~= "table" then error("managed preset contains an unsafe value") end
    if getmetatable(value) ~= nil then error("managed preset table must not have a metatable") end
    for key, child in pairs(value) do
        local key_type = type(key)
        if key_type == "number" then
            if key < 1 or key ~= math.floor(key) then error("managed preset contains an invalid list key") end
        elseif key_type == "string" then
            if #key < 1 or #key > 128 or not key:match("^[A-Za-z][A-Za-z0-9_]*$") then
                error("managed preset contains an invalid develop setting name")
            end
        else
            error("managed preset contains an unsafe table key")
        end
        validate_develop_value(child, state, depth + 1)
    end
end

local function parse_managed_registry_literal(contents)
    -- Parse only the deterministic data-literal subset emitted by
    -- style_library._lua_literal. Registry bytes are never compiled or
    -- executed, even on runtimes that expose dynamic compilation helpers.
    local cursor = 1
    local length = #contents
    local nodes = 0
    local parse_value
    local string_byte = string.byte
    local string_char = string.char
    local string_sub = string.sub
    local table_concat = table.concat
    local MAX_LITERAL_STRING_BYTES = 1024 * 1024

    local function syntax_error(message)
        error("managed preset registry syntax error at byte " .. tostring(cursor) .. ": " .. message)
    end

    local function byte_at(position)
        return string_byte(contents, position or cursor)
    end

    local function is_space(byte)
        return byte == 32 or (byte ~= nil and byte >= 9 and byte <= 13)
    end

    local function is_digit(byte)
        return byte ~= nil and byte >= 48 and byte <= 57
    end

    local function is_identifier_start(byte)
        return byte == 95
            or (byte ~= nil and byte >= 65 and byte <= 90)
            or (byte ~= nil and byte >= 97 and byte <= 122)
    end

    local function is_identifier_continue(byte)
        return is_identifier_start(byte) or is_digit(byte)
    end

    local function skip_space()
        while is_space(byte_at()) do cursor = cursor + 1 end
    end

    local function expect_byte(expected, label)
        if byte_at() ~= expected then syntax_error("expected " .. label) end
        cursor = cursor + 1
    end

    local function parse_string()
        expect_byte(34, '"')
        local start = cursor
        local pieces = nil
        local decoded_bytes = 0
        while cursor <= length do
            local byte = byte_at()
            if byte == 34 then
                local chunk_length = cursor - start
                if pieces == nil then
                    if chunk_length > MAX_LITERAL_STRING_BYTES then syntax_error("string is too large") end
                    local value = string_sub(contents, start, cursor - 1)
                    cursor = cursor + 1
                    return value
                end
                if chunk_length > 0 then
                    pieces[#pieces + 1] = string_sub(contents, start, cursor - 1)
                    decoded_bytes = decoded_bytes + chunk_length
                end
                if decoded_bytes > MAX_LITERAL_STRING_BYTES then syntax_error("string is too large") end
                cursor = cursor + 1
                return table_concat(pieces)
            elseif byte == 92 then
                local chunk_length = cursor - start
                decoded_bytes = decoded_bytes + chunk_length
                if decoded_bytes > MAX_LITERAL_STRING_BYTES then syntax_error("string is too large") end
                if pieces == nil then pieces = {} end
                if chunk_length > 0 then
                    pieces[#pieces + 1] = string_sub(contents, start, cursor - 1)
                end
                cursor = cursor + 1
                if cursor > length then syntax_error("unterminated string escape") end
                local escaped = byte_at()
                if escaped == 92 or escaped == 34 then
                    pieces[#pieces + 1] = string_char(escaped)
                    cursor = cursor + 1
                elseif escaped == 110 or escaped == 114 or escaped == 116 then
                    pieces[#pieces + 1] = escaped == 110 and "\n" or (escaped == 114 and "\r" or "\t")
                    cursor = cursor + 1
                elseif is_digit(escaped) then
                    local second = byte_at(cursor + 1)
                    local third = byte_at(cursor + 2)
                    if not is_digit(second) or not is_digit(third) then
                        syntax_error("decimal string escapes require exactly three digits")
                    end
                    local code = (escaped - 48) * 100 + (second - 48) * 10 + (third - 48)
                    if code > 255 then syntax_error("decimal string escape is out of range") end
                    pieces[#pieces + 1] = string_char(code)
                    cursor = cursor + 3
                else
                    syntax_error("unsupported string escape")
                end
                decoded_bytes = decoded_bytes + 1
                if decoded_bytes > MAX_LITERAL_STRING_BYTES then syntax_error("string is too large") end
                start = cursor
            else
                if byte < 32 then syntax_error("unescaped control character in string") end
                cursor = cursor + 1
                if cursor - start > MAX_LITERAL_STRING_BYTES then syntax_error("string is too large") end
            end
        end
        syntax_error("unterminated string")
    end

    local function parse_number()
        local start = cursor
        if byte_at() == 45 then cursor = cursor + 1 end
        if not is_digit(byte_at()) then syntax_error("invalid number") end
        if byte_at() == 48 then
            cursor = cursor + 1
            if is_digit(byte_at()) then syntax_error("number contains a leading zero") end
        else
            while is_digit(byte_at()) do cursor = cursor + 1 end
        end
        if byte_at() == 46 then
            cursor = cursor + 1
            if not is_digit(byte_at()) then syntax_error("fraction is missing digits") end
            while is_digit(byte_at()) do cursor = cursor + 1 end
        end
        if byte_at() == 101 or byte_at() == 69 then
            cursor = cursor + 1
            if byte_at() == 43 or byte_at() == 45 then cursor = cursor + 1 end
            if not is_digit(byte_at()) then syntax_error("exponent is missing digits") end
            while is_digit(byte_at()) do cursor = cursor + 1 end
        end
        local number = tonumber(string_sub(contents, start, cursor - 1))
        if not number or number ~= number or number == math.huge or number == -math.huge then
            syntax_error("number is not finite")
        end
        return number
    end

    local function parse_table(depth)
        if depth > 12 then syntax_error("tables are nested too deeply") end
        expect_byte(123, "{")
        skip_space()
        local result = {}
        local seen_keys = {}
        local next_index = 1
        while byte_at() ~= 125 do
            if cursor > length then syntax_error("unterminated table") end
            local key = nil
            if byte_at() == 91 then
                cursor = cursor + 1
                skip_space()
                if byte_at() ~= 34 then syntax_error("bracket keys must be strings") end
                key = parse_string()
                skip_space()
                expect_byte(93, "]")
                skip_space()
                expect_byte(61, "=")
                skip_space()
            elseif is_identifier_start(byte_at()) then
                local saved = cursor
                cursor = cursor + 1
                while is_identifier_continue(byte_at()) do cursor = cursor + 1 end
                local identifier_end = cursor - 1
                skip_space()
                if byte_at() == 61 then
                    key = string_sub(contents, saved, identifier_end)
                    cursor = cursor + 1
                    skip_space()
                else
                    cursor = saved
                end
            end
            local child = parse_value(depth + 1)
            if key ~= nil then
                if seen_keys[key] then syntax_error("duplicate table key") end
                seen_keys[key] = true
                result[key] = child
            else
                result[next_index] = child
                next_index = next_index + 1
            end
            skip_space()
            if byte_at() == 44 then
                cursor = cursor + 1
                skip_space()
            elseif byte_at() ~= 125 then
                syntax_error("expected comma or closing brace")
            end
        end
        cursor = cursor + 1
        return result
    end

    local function keyword(word, value)
        if string_sub(contents, cursor, cursor + #word - 1) ~= word then return false, nil end
        if is_identifier_continue(byte_at(cursor + #word)) then syntax_error("invalid keyword") end
        cursor = cursor + #word
        return true, value
    end

    parse_value = function(depth)
        skip_space()
        nodes = nodes + 1
        if nodes > 200000 then syntax_error("registry contains too many values") end
        local byte = byte_at()
        if byte == 123 then return parse_table(depth) end
        if byte == 34 then return parse_string() end
        if byte == 45 or is_digit(byte) then return parse_number() end
        local matched, value = keyword("true", true)
        if matched then return value end
        matched, value = keyword("false", false)
        if matched then return value end
        matched = keyword("nil", nil)
        if matched then return nil end
        syntax_error("unexpected value")
    end

    skip_space()
    if string_sub(contents, cursor, cursor + 5) ~= "return" then syntax_error("missing return prefix") end
    cursor = cursor + 6
    if is_identifier_continue(byte_at()) then syntax_error("invalid return prefix") end
    local payload = parse_value(0)
    skip_space()
    if cursor <= length then syntax_error("trailing content") end
    return payload
end

local function load_managed_registry(path)
    local contents = read_all(path, MAX_MANAGED_REGISTRY_BYTES)
    local payload = parse_managed_registry_literal(contents)
    if type(payload) ~= "table" or getmetatable(payload) ~= nil then
        error("managed preset registry must return a plain table")
    end
    if payload.schema_version ~= 1 then error("unsupported managed preset registry schema") end
    local registry_hash = payload.registry_hash
    if type(registry_hash) ~= "string" or #registry_hash ~= 64 or not registry_hash:match("^%x+$") then
        error("managed preset registry hash is invalid")
    end
    if type(payload.entries) ~= "table" or getmetatable(payload.entries) ~= nil then
        error("managed preset registry entries must be a plain list")
    end
    local count = 0
    for key in pairs(payload.entries) do
        if type(key) ~= "number" or key < 1 or key ~= math.floor(key) then
            error("managed preset registry entries must be a dense list")
        end
        count = count + 1
    end
    if count ~= #payload.entries or count > MAX_MANAGED_PRESETS then
        error("managed preset registry entry count is invalid")
    end

    local entries = {}
    local names = {}
    local identities = {}
    for _, raw in ipairs(payload.entries) do
        if type(raw) ~= "table" or getmetatable(raw) ~= nil then
            error("managed preset registry entry must be a plain table")
        end
        local preset_id = raw.preset_id
        local file_hash = raw.file_hash
        local plugin_name = raw.plugin_name
        if type(preset_id) ~= "string" or #preset_id < 1 or #preset_id > 256 then
            error("managed preset registry contains an invalid preset_id")
        end
        if type(file_hash) ~= "string" or #file_hash ~= 64 or not file_hash:match("^%x+$") then
            error("managed preset registry contains an invalid file hash")
        end
        if type(plugin_name) ~= "string" or #plugin_name < 1 or #plugin_name > 255 then
            error("managed preset registry contains an invalid plugin name")
        end
        if raw.target_scope ~= "plugin" then error("managed preset registry scope must be plugin") end
        local source_id = tostring(raw.source_id or raw.source or ""):lower()
        local license = tostring(raw.license or "")
        if source_id:find("adobe%-local", 1, false)
            or license == "LicenseRef-Adobe-Proprietary-Local-Install" then
            error("Adobe local-only presets must not enter the managed plugin registry")
        end
        if names[plugin_name] then error("managed preset registry contains a duplicate plugin name") end
        local identity = preset_id .. "@" .. file_hash:lower()
        if identities[identity] then error("managed preset registry contains a duplicate identity") end
        if type(raw.develop_settings) ~= "table" then
            error("managed preset registry entry is missing develop settings")
        end
        validate_develop_value(raw.develop_settings, { nodes = 0 }, 0)
        names[plugin_name] = true
        identities[identity] = true
        table.insert(entries, {
            preset_id = preset_id,
            file_hash = file_hash:lower(),
            plugin_name = plugin_name,
            develop_settings = raw.develop_settings,
        })
    end
    table.sort(entries, function(left, right) return left.plugin_name < right.plugin_name end)
    return { registry_hash = registry_hash:lower(), entries = entries }
end

function Bridge.new(plugin_path)
    local self = setmetatable({}, Bridge)
    self.plugin_path = plugin_path
    self.running = false
    self.managed_registry_hash = nil
    self.managed_registration_entries = nil
    self.managed_registry_signature = nil
    self.next_managed_registry_check = 0
    local pointer, configured_style_root = read_bridge_pointer(
        LrPathUtils.child(plugin_path, "bridge-path.txt")
    )
    if pointer and pointer ~= "" and pointer:match("^%a:[/\\]")
        and not has_relative_segment(pointer)
        and path_key(LrPathUtils.leafName(pointer)) == "lightroom-bridge" then
        self.root = pointer
        self.paths = {
            pending = LrPathUtils.child(pointer, "pending"),
            running = LrPathUtils.child(pointer, "running"),
            done = LrPathUtils.child(pointer, "done"),
            failed = LrPathUtils.child(pointer, "failed"),
            cancelled = LrPathUtils.child(pointer, "cancelled"),
            backups = LrPathUtils.child(pointer, "backups"),
            logs = LrPathUtils.child(pointer, "logs"),
            previews = LrPathUtils.child(pointer, "previews"),
            exports = LrPathUtils.child(pointer, "exports"),
            presets = LrPathUtils.child(pointer, "presets"),
            looks = LrPathUtils.child(LrPathUtils.child(pointer, "presets"), "looks"),
            heartbeat = LrPathUtils.child(pointer, "heartbeat.line"),
        }
        do
            local data_root = LrPathUtils.parent(pointer)
            local style_root = nil
            if data_root and path_key(LrPathUtils.leafName(data_root)) == "state" then
                local content_root = LrPathUtils.parent(data_root)
                style_root = content_root and LrPathUtils.child(
                    LrPathUtils.child(content_root, "styles"), "style-library"
                ) or nil
            else
                style_root = data_root and LrPathUtils.child(data_root, "style-library") or nil
            end
            if configured_style_root and configured_style_root ~= "" then
                if not configured_style_root:match("^%a:[/\\]")
                    or has_relative_segment(configured_style_root)
                    or not style_root
                    or path_key(configured_style_root) ~= path_key(style_root) then
                    style_root = nil
                else
                    style_root = configured_style_root
                end
            end
            local registry_path = style_root and LrPathUtils.child(style_root, MANAGED_REGISTRY_FILE) or nil
            local state_path = style_root and LrPathUtils.child(style_root, MANAGED_REGISTRATION_FILE) or nil
            if style_root and registry_path and state_path
                and path_key(LrPathUtils.parent(registry_path)) == path_key(style_root)
                and path_key(LrPathUtils.parent(state_path)) == path_key(style_root) then
                self.managed_paths = {
                    data_root = data_root,
                    style_root = style_root,
                    registry = registry_path,
                    registration = state_path,
                }
            end
        end
    end
    return self
end

function Bridge:log(level, message)
    if not self.paths then return end
    local log_path = LrPathUtils.child(self.paths.logs, "plugin.log")
    local handle = io.open(log_path, "ab")
    if handle then
        handle:write(now_utc() .. "\t" .. level .. "\t" .. safe_message(message) .. "\n")
        handle:close()
    end
end

function Bridge:heartbeat(state)
    if not self.paths then return end
    local parts = {
        HEARTBEAT_PROTOCOL,
        "at=" .. encode_scalar(now_utc()),
        "state=" .. encode_scalar(state),
        "plugin_version=" .. encode_scalar(PLUGIN_VERSION),
    }
    local catalog_ok, catalog_path = pcall(function()
        return LrApplication.activeCatalog():getPath()
    end)
    if catalog_ok and catalog_path and catalog_path ~= "" then
        table.insert(parts, "catalog_path=" .. encode_scalar(catalog_path))
    end
    local version_ok, lightroom_version = pcall(function()
        local version = LrApplication.versionTable()
        return table.concat({
            tostring(version.major or 0),
            tostring(version.minor or 0),
            tostring(version.revision or 0),
            tostring(version.build or 0),
        }, ".")
    end)
    if version_ok and lightroom_version and lightroom_version ~= "" then
        table.insert(parts, "lightroom_version=" .. encode_scalar(lightroom_version))
    end
    atomic_write(self.paths.heartbeat, table.concat(parts, "\t") .. "\n")
end

function Bridge:ensure_layout()
    if not self.paths then return false end
    for _, path in pairs({
        self.paths.pending,
        self.paths.running,
        self.paths.done,
        self.paths.failed,
        self.paths.cancelled,
        self.paths.backups,
        self.paths.logs,
        self.paths.previews,
        self.paths.exports,
        self.paths.presets,
        self.paths.looks,
    }) do
        LrFileUtils.createAllDirectories(path)
    end
    return true
end

function Bridge:is_cancelled(task)
    if not task or not valid_identifier(task.batch_id) then return false end
    return LrFileUtils.exists(LrPathUtils.child(self.paths.cancelled, task.batch_id .. ".cancel"))
end

function Bridge:ensure_not_cancelled(task, phase)
    if self:is_cancelled(task) then
        error(CANCEL_ERROR .. ": " .. tostring(phase or "operation"))
    end
end

function Bridge:claim(path)
    local target = LrPathUtils.child(self.paths.running, LrPathUtils.leafName(path))
    if LrFileUtils.exists(target) then return nil end
    local ok = pcall(function() LrFileUtils.move(path, target) end)
    if ok and LrFileUtils.exists(target) then return target end
    return nil
end

function Bridge:assert_photo_identity(photo, photo_path, phase)
    if not photo then error("missing Lightroom photo object during " .. phase) end
    local actual = photo:getRawMetadata("path")
    if path_key(actual) ~= path_key(photo_path) then
        error("Lightroom photo path changed during " .. phase .. ": " .. tostring(actual))
    end
    local located = LrApplication.activeCatalog():findPhotoByPath(photo_path)
    if not located or not same_photo(located, photo) then
        error("Lightroom catalog no longer resolves the exact photo during " .. phase)
    end
end

function Bridge:assert_preview_catalog_identity(context, phase)
    local catalog = LrApplication.activeCatalog()
    local expected_path = context.preview_catalog_path
        or (context.preview_record and context.preview_record.catalog_path)
    if not expected_path then error("missing preview catalog identity during " .. phase) end
    if context.preview_catalog and catalog ~= context.preview_catalog then
        error("active Lightroom catalog object changed during " .. phase
            .. "; preview isolation record retained")
    end
    if path_key(catalog:getPath()) ~= path_key(expected_path) then
        error("active Lightroom catalog path changed during " .. phase
            .. "; preview isolation record retained")
    end
    return catalog
end

function Bridge:find_existing_photo(task)
    self:ensure_not_cancelled(task, "before repair catalog lookup")
    if not LrFileUtils.exists(task.photo_path) then
        error("photo does not exist: " .. task.photo_path)
    end
    local photo = LrApplication.activeCatalog():findPhotoByPath(task.photo_path)
    if not photo then
        error("metadata repair refuses to import a photo that is not already in the Lightroom catalog")
    end
    self:assert_photo_identity(photo, task.photo_path, "repair catalog lookup")
    self:ensure_not_cancelled(task, "after repair catalog lookup")
    return photo
end

function Bridge:assert_master_identity(photo, task, context, phase)
    if context and context.preview_isolated then
        self:assert_preview_catalog_identity(context, phase .. " catalog check")
    end
    self:assert_photo_identity(photo, task.photo_path, phase)
    if photo:getRawMetadata("isVirtualCopy") then
        error("preview source became a virtual copy during " .. phase)
    end
    local source_uuid = tostring(photo:getRawMetadata("uuid") or "")
    local source_local_id = tonumber(photo.localIdentifier)
    if not uuid_identity(source_uuid) or not source_local_id then
        error("preview source identity is incomplete during " .. phase)
    end
    if context and context.source_uuid and source_uuid ~= context.source_uuid then
        error("preview source UUID changed during " .. phase)
    end
    if context and context.source_local_id and source_local_id ~= context.source_local_id then
        error("preview source local identifier changed during " .. phase)
    end
end

function Bridge:find_existing_master(task, context)
    self:ensure_not_cancelled(task, "before preview catalog lookup")
    if not LrFileUtils.exists(task.photo_path) then
        error("photo does not exist: " .. task.photo_path)
    end
    local catalog = LrApplication.activeCatalog()
    context.preview_catalog = catalog
    context.preview_catalog_path = catalog:getPath()
    context.preview_isolated = true
    self:assert_preview_catalog_identity(context, "preview catalog lookup")
    local photo = catalog:findPhotoByPath(task.photo_path)
    if not photo then
        error("isolated preview refuses to import a photo that is not already in the Lightroom catalog")
    end
    context.source_uuid = tostring(photo:getRawMetadata("uuid") or "")
    context.source_local_id = tonumber(photo.localIdentifier)
    self:assert_master_identity(photo, task, context, "preview catalog lookup")
    self:ensure_not_cancelled(task, "after preview catalog lookup")
    return photo
end

function Bridge:preview_copy_record_paths(batch_id, task_id)
    if not valid_identifier(batch_id) or not valid_identifier(task_id) then
        error("invalid preview isolation identifier")
    end
    local stem = batch_id .. "--" .. task_id .. ".preview-copy"
    return {
        intent = LrPathUtils.child(self.paths.backups, stem .. ".intent"),
        started = LrPathUtils.child(self.paths.backups, stem .. ".started"),
        selection_attempted = LrPathUtils.child(
            self.paths.backups,
            stem .. ".selection-restore-attempted"
        ),
        active = LrPathUtils.child(self.paths.backups, stem .. ".state"),
        removed = LrPathUtils.child(self.paths.backups, stem .. ".removed"),
    }
end

function Bridge:read_preview_copy_record(path, expected_phase)
    local fields = parse_line(read_first_line(path), PREVIEW_COPY_PROTOCOL)
    local allowed = {
        phase = true, batch_id = true, task_id = true, catalog_path = true,
        photo_path = true, source_uuid = true, source_local_id = true,
        working_uuid = true, working_local_id = true, copy_name = true,
        selection_active_uuid = true, selection_other_uuids = true,
        creation_started = true, created_at = true, removed_at = true,
    }
    for key, _ in pairs(fields) do
        if not allowed[key] then error("unsupported preview isolation field: " .. tostring(key)) end
    end
    for _, key in ipairs({
        "phase", "batch_id", "task_id", "catalog_path", "photo_path",
        "source_uuid", "source_local_id", "selection_active_uuid",
        "selection_other_uuids", "copy_name", "created_at",
    }) do
        if fields[key] == nil then error("missing preview isolation field: " .. key) end
    end
    if fields.phase ~= expected_phase then error("unexpected preview isolation phase") end
    if not valid_identifier(fields.batch_id) or not valid_identifier(fields.task_id) then
        error("invalid preview isolation task identity")
    end
    if not absolute_windows_path(fields.catalog_path) or has_relative_segment(fields.catalog_path) then
        error("invalid preview isolation catalog path")
    end
    if not absolute_windows_path(fields.photo_path) or has_relative_segment(fields.photo_path) then
        error("invalid preview isolation photo path")
    end
    if not uuid_identity(fields.source_uuid) then error("invalid preview source UUID") end
    local source_local_id = tonumber(fields.source_local_id)
    if not source_local_id or source_local_id < 1 or source_local_id ~= math.floor(source_local_id) then
        error("invalid preview source local identifier")
    end
    fields.source_local_id = source_local_id
    if type(fields.selection_active_uuid) ~= "string"
        or (fields.selection_active_uuid ~= "" and not uuid_identity(fields.selection_active_uuid)) then
        error("invalid preview isolation active selection UUID")
    end
    if type(fields.selection_other_uuids) ~= "string" or #fields.selection_other_uuids > 1000000 then
        error("invalid preview isolation selection list")
    end
    local selection_count = 0
    local seen_selection = {}
    local canonical_selection = {}
    for uuid in fields.selection_other_uuids:gmatch("[^,]+") do
        if not uuid_identity(uuid) or seen_selection[uuid]
            or uuid == fields.selection_active_uuid then
            error("invalid preview isolation selection member")
        end
        seen_selection[uuid] = true
        table.insert(canonical_selection, uuid)
        selection_count = selection_count + 1
        if selection_count > MAX_PREVIEW_SELECTION then
            error("preview isolation selection list is too large")
        end
    end
    if fields.selection_active_uuid == "" and selection_count > 0 then
        error("preview isolation selection has members but no active photo")
    end
    if table.concat(canonical_selection, ",") ~= fields.selection_other_uuids then
        error("preview isolation selection list is not canonical")
    end
    if fields.copy_name ~= preview_copy_name(fields.batch_id, fields.task_id) then
        error("preview isolation copy name does not match its task identity")
    end
    local legacy_creation_state_unknown = fields.creation_started == nil
    if legacy_creation_state_unknown then
        -- Protocol /1 existed briefly before creation-started was durable.
        -- An old intent is treated as possibly started and therefore retained;
        -- active records are necessarily started, while a no-UUID removed
        -- record represents the pre-create case.
        fields.creation_started = expected_phase ~= "removed" or fields.working_uuid ~= nil
        fields.creation_state_legacy_unknown = true
    elseif type(fields.creation_started) ~= "boolean" then
        error("invalid preview isolation creation-started state")
    end
    if expected_phase == "intent" and fields.creation_started
        and not legacy_creation_state_unknown then
        error("preview intent cannot claim virtual-copy creation started")
    end
    if (expected_phase == "started" or expected_phase == "selection_restore_attempted"
        or expected_phase == "active")
        and not fields.creation_started then
        error("preview isolation phase requires creation-started state")
    end
    if type(fields.created_at) ~= "string" or fields.created_at == "" then
        error("invalid preview isolation creation time")
    end
    if expected_phase == "active" or fields.working_uuid ~= nil or fields.working_local_id ~= nil then
        if not uuid_identity(fields.working_uuid) then error("invalid preview working UUID") end
        local working_local_id = tonumber(fields.working_local_id)
        if not working_local_id or working_local_id < 1 or working_local_id ~= math.floor(working_local_id) then
            error("invalid preview working local identifier")
        end
        fields.working_local_id = working_local_id
        if fields.working_uuid == fields.selection_active_uuid
            or seen_selection[fields.working_uuid] then
            error("preview working UUID appears in its saved prior selection")
        end
    end
    if (expected_phase == "intent" or expected_phase == "started"
        or expected_phase == "selection_restore_attempted")
        and (fields.working_uuid ~= nil or fields.working_local_id ~= nil) then
        error("preview pre-identity record unexpectedly contains a working identity")
    end
    if expected_phase == "removed" and (
        type(fields.removed_at) ~= "string" or fields.removed_at == ""
    ) then
        error("invalid preview isolation removal time")
    end
    local phase_suffix = expected_phase
    if expected_phase == "active" then
        phase_suffix = "state"
    elseif expected_phase == "selection_restore_attempted" then
        phase_suffix = "selection-restore-attempted"
    end
    local suffix = ".preview-copy." .. phase_suffix
    local expected_leaf = fields.batch_id .. "--" .. fields.task_id .. suffix
    if LrPathUtils.leafName(path) ~= expected_leaf then
        error("preview isolation record filename does not match its identity")
    end
    return fields
end

function Bridge:assert_preview_record_lineage(left, right, phase)
    if not left or not right then error("missing preview isolation lineage during " .. phase) end
    for _, key in ipairs({
        "batch_id", "task_id", "source_uuid", "source_local_id",
        "selection_active_uuid", "selection_other_uuids", "copy_name", "created_at",
    }) do
        if left[key] ~= right[key] then
            error("preview isolation lineage mismatch for " .. key .. " during " .. phase)
        end
    end
    if path_key(left.catalog_path) ~= path_key(right.catalog_path)
        or path_key(left.photo_path) ~= path_key(right.photo_path) then
        error("preview isolation path lineage mismatch during " .. phase)
    end
    if left.working_uuid ~= nil and right.working_uuid ~= nil then
        if left.working_uuid ~= right.working_uuid
            or left.working_local_id ~= right.working_local_id then
            error("preview isolation working identity mismatch during " .. phase)
        end
    end
end

function Bridge:preview_selection_restore_was_attempted(paths, record, phase)
    if not LrFileUtils.exists(paths.selection_attempted) then return false end
    local attempted = self:read_preview_copy_record(
        paths.selection_attempted,
        "selection_restore_attempted"
    )
    self:assert_preview_record_lineage(attempted, record, phase)
    return true
end

function Bridge:record_preview_selection_restore_attempt(paths, record)
    if self:preview_selection_restore_was_attempted(
        paths,
        record,
        "existing preview selection-restore attempt"
    ) then
        return false
    end
    local attempted = {}
    for key, value in pairs(record) do attempted[key] = value end
    attempted.phase = "selection_restore_attempted"
    attempted.creation_started = true
    attempted.working_uuid = nil
    attempted.working_local_id = nil
    atomic_create(paths.selection_attempted, preview_copy_record_line(attempted))
    return true
end

function Bridge:clear_preview_selection_restore_attempt(paths, record, phase)
    if not LrFileUtils.exists(paths.selection_attempted) then return end
    local attempted = self:read_preview_copy_record(
        paths.selection_attempted,
        "selection_restore_attempted"
    )
    self:assert_preview_record_lineage(attempted, record, phase)
    delete_if_exists(paths.selection_attempted)
end

function Bridge:archive_preview_copy_record(context, source_path)
    local record = context.preview_record
    if not record then error("missing preview isolation record while archiving") end
    local paths = self:preview_copy_record_paths(record.batch_id, record.task_id)
    if not source_path then error("missing preview isolation source record while archiving") end
    if not LrFileUtils.exists(source_path) then
        if not LrFileUtils.exists(paths.removed) then
            error("preview isolation source record disappeared before durable removal archive")
        end
        local existing = self:read_preview_copy_record(paths.removed, "removed")
        self:assert_preview_record_lineage(
            existing,
            record,
            "preview isolation missing-source archive convergence"
        )
        context.preview_record = existing
        return
    end
    local archived = {}
    for key, value in pairs(record) do archived[key] = value end
    archived.phase = "removed"
    archived.removed_at = now_utc()
    if not LrFileUtils.exists(paths.removed) then
        atomic_create(paths.removed, preview_copy_record_line(archived))
    else
        local existing = self:read_preview_copy_record(paths.removed, "removed")
        self:assert_preview_record_lineage(
            existing,
            archived,
            "preview isolation archive convergence"
        )
    end
    self:clear_preview_selection_restore_attempt(
        paths,
        record,
        "preview isolation archive selection-attempt convergence"
    )
    delete_if_exists(source_path)
    context.preview_record = archived
end

function Bridge:capture_selection(catalog, excluded_photo)
    local active = catalog:getTargetPhoto()
    local selected = {}
    if active then
        for _, photo in ipairs(catalog:getTargetPhotos() or {}) do
            if not excluded_photo or not same_photo(photo, excluded_photo) then
                local duplicate = false
                for _, prior in ipairs(selected) do
                    if same_photo(prior, photo) then duplicate = true end
                end
                if not duplicate then table.insert(selected, photo) end
            end
        end
    end
    if active and excluded_photo and same_photo(active, excluded_photo) then active = nil end
    if not active and #selected > 0 then active = selected[1] end
    local others = {}
    for _, photo in ipairs(selected) do
        if not active or not same_photo(photo, active) then table.insert(others, photo) end
    end
    return { active = active, others = others }
end

function Bridge:selection_record_values(selection)
    local active_uuid = ""
    if selection.active then
        active_uuid = tostring(selection.active:getRawMetadata("uuid") or "")
        if not uuid_identity(active_uuid) then error("selected active photo has no stable UUID") end
    end
    local other_uuids = {}
    for _, photo in ipairs(selection.others or {}) do
        local uuid = tostring(photo:getRawMetadata("uuid") or "")
        if not uuid_identity(uuid) or uuid == active_uuid then
            error("selected photo has an invalid or duplicate UUID")
        end
        for _, prior in ipairs(other_uuids) do
            if prior == uuid then error("selected photo UUID is duplicated") end
        end
        table.insert(other_uuids, uuid)
        if #other_uuids > MAX_PREVIEW_SELECTION then
            error("Lightroom selection is too large to restore safely")
        end
    end
    return active_uuid, table.concat(other_uuids, ",")
end

function Bridge:selection_from_record(catalog, record, allow_missing)
    local selection = { active = nil, others = {} }
    local missing = {}
    if record.selection_active_uuid ~= "" then
        selection.active = catalog:findPhotoByUuid(record.selection_active_uuid)
        if not selection.active then
            if not allow_missing then error("recorded active selection photo no longer exists") end
            table.insert(missing, record.selection_active_uuid)
        end
    end
    for uuid in record.selection_other_uuids:gmatch("[^,]+") do
        local photo = catalog:findPhotoByUuid(uuid)
        if not photo then
            if not allow_missing then error("recorded selected photo no longer exists: " .. uuid) end
            table.insert(missing, uuid)
        else
            if selection.active and same_photo(photo, selection.active) then
                error("recorded selection duplicates its active photo")
            end
            for _, prior in ipairs(selection.others) do
                if same_photo(photo, prior) then error("recorded selection contains a duplicate photo") end
            end
            table.insert(selection.others, photo)
        end
    end
    if not selection.active and #selection.others > 0 then
        selection.active = table.remove(selection.others, 1)
    end
    if not selection.active and #selection.others > 0 then
        error("recorded selection has members but no active photo")
    end
    return selection, missing
end

function Bridge:verify_selection(catalog, selection, phase)
    local actual_active = catalog:getTargetPhoto()
    if not selection.active then
        if actual_active then error("Lightroom selection was not cleared during " .. phase) end
        return
    end
    if not same_photo(actual_active, selection.active) then
        error("Lightroom active photo changed during " .. phase)
    end
    local expected = { selection.active }
    for _, photo in ipairs(selection.others or {}) do table.insert(expected, photo) end
    local actual = catalog:getTargetPhotos() or {}
    if #actual ~= #expected then error("Lightroom selection count changed during " .. phase) end
    for _, wanted in ipairs(expected) do
        local found = false
        for _, photo in ipairs(actual) do
            if same_photo(wanted, photo) then found = true end
        end
        if not found then error("Lightroom selection membership changed during " .. phase) end
    end
end

function Bridge:verify_preview_single_selection(context, catalog, photo, phase)
    local active_catalog = self:assert_preview_catalog_identity(context, phase .. " catalog check")
    if active_catalog ~= catalog then
        error("isolated preview catalog object changed during " .. phase)
    end
    local active = active_catalog:getTargetPhoto()
    local selected = active_catalog:getTargetPhotos() or {}
    if not same_photo(active, photo) or #selected ~= 1 or not same_photo(selected[1], photo) then
        error("isolated virtual copy is not the sole exact selection during " .. phase)
    end
end

function Bridge:select_only(catalog, photo, phase)
    -- setSelectedPhotos can converge asynchronously when the requested photo
    -- is in another folder. Preview creation is selection-addressed, so use
    -- the same bounded exact-selection convergence loop as restoration before
    -- createVirtualCopies is allowed to run.
    self:restore_selection(catalog, { active = photo, others = {} }, phase)
end

function Bridge:restore_selection(catalog, selection, phase)
    -- Lightroom can update the active/target selection asynchronously after a
    -- virtual-copy operation, especially when the next preview comes from a
    -- different folder.  Keep the exact UUID/object verification, but allow a
    -- short bounded convergence window instead of treating the first stale UI
    -- read as a destructive-selection failure.
    local last_error = nil
    for poll = 1, 40 do
        if selection.active then
            catalog:setSelectedPhotos(selection.active, selection.others or {})
        else
            LrSelection.selectNone()
        end
        local verified, verify_error = LrTasks.pcall(function()
            self:verify_selection(catalog, selection, phase)
        end)
        if verified then return end
        last_error = verify_error
        if poll < 40 then LrTasks.sleep(0.05) end
    end
    error("Lightroom selection did not stabilize during " .. phase .. ": "
        .. safe_message(last_error))
end

function Bridge:activate_preview_library_context(context, catalog, phase)
    if context.preview_library_context_active then return end
    local active_sources = catalog:getActiveSources()
    if type(active_sources) ~= "table" or #active_sources < 1 then
        error("Lightroom active sources are unavailable during " .. phase)
    end
    local view_filter = catalog:getCurrentViewFilter()
    if type(view_filter) ~= "table" then
        error("Lightroom view filter is unavailable during " .. phase)
    end
    context.preview_active_sources_before = active_sources
    context.preview_view_filter_before = view_filter
    context.preview_library_context_active = true

    -- A photo outside the user's current folder/collection or hidden by a
    -- Library filter cannot become the SDK target.  Preview work therefore
    -- opens a temporary all-photos, unfiltered filmstrip, then restores the
    -- exact prior source/filter/selection before returning to the user.
    if catalog:setActiveSources(catalog.kAllPhotos) ~= true then
        error("Lightroom refused the temporary All Photographs preview source")
    end
    local open_filter = {}
    for key, value in pairs(view_filter) do open_filter[key] = value end
    open_filter.filtersActive = false
    open_filter.columnBrowserActive = false
    open_filter.searchStringActive = false
    if catalog:setViewFilter(open_filter) == nil then
        error("Lightroom refused the temporary unfiltered preview view")
    end
end

function Bridge:restore_preview_library_context(context, catalog, selection, phase)
    if not context.preview_library_context_active then return true, nil end
    local restored, restore_error = LrTasks.pcall(function()
        self:assert_preview_catalog_identity(context, phase .. " catalog check")
        if catalog:setActiveSources(context.preview_active_sources_before) ~= true then
            error("Lightroom refused to restore the prior Library source")
        end
        if catalog:setViewFilter(context.preview_view_filter_before) == nil then
            error("Lightroom refused to restore the prior Library filter")
        end
        self:restore_selection(catalog, selection, phase)
    end)
    if not restored then return false, safe_message(restore_error) end
    context.preview_library_context_active = false
    context.preview_library_context_restored = true
    context.preview_selection_restored = true
    return true, nil
end

function Bridge:restore_saved_preview_selection(context, catalog, phase)
    local selection = context.selection_restore_override or context.selection_before_create
    if not selection then return true, nil end
    if context.preview_selection_restored and not context.preview_library_context_active then
        return true, nil
    end
    if context.preview_library_context_active then
        return self:restore_preview_library_context(context, catalog, selection, phase)
    end
    local restored, restore_error = LrTasks.pcall(function()
        self:assert_preview_catalog_identity(context, phase .. " catalog check")
        self:restore_selection(catalog, selection, phase)
    end)
    if not restored then return false, safe_message(restore_error) end
    context.preview_selection_restored = true
    return true, nil
end

function Bridge:restore_ambiguous_preview_selection_once(context, catalog, paths, record, phase)
    if self:preview_selection_restore_was_attempted(
        paths,
        record,
        phase .. " prior-attempt check"
    ) then
        return false, {}
    end
    local selection, missing = self:selection_from_record(catalog, record, true)
    context.selection_restore_override = selection
    self:assert_preview_catalog_identity(context, phase .. " pre-attempt catalog check")
    self:record_preview_selection_restore_attempt(paths, record)
    local restored, restore_error = self:restore_saved_preview_selection(
        context,
        catalog,
        phase
    )
    if not restored then
        error("one-time ambiguous preview selection restore failed: " .. restore_error
            .. "; attempt marker retained")
    end
    return true, missing
end

function Bridge:assert_preview_copy_identity(photo, task, context, phase)
    if not photo then error("missing isolated virtual copy during " .. phase) end
    if photo:getRawMetadata("isVirtualCopy") ~= true then
        error("isolated preview target is not a virtual copy during " .. phase)
    end
    local catalog = self:assert_preview_catalog_identity(context, phase .. " catalog check")
    local actual_path = photo:getRawMetadata("path")
    if path_key(actual_path) ~= path_key(task.photo_path) then
        error("isolated preview path changed during " .. phase)
    end
    local working_uuid = tostring(photo:getRawMetadata("uuid") or "")
    if not uuid_identity(working_uuid) or working_uuid ~= context.working_uuid then
        error("isolated preview UUID changed during " .. phase)
    end
    if tonumber(photo.localIdentifier) ~= context.working_local_id then
        error("isolated preview local identifier changed during " .. phase)
    end
    local located = catalog:findPhotoByUuid(context.working_uuid)
    if not located or not same_photo(located, photo) then
        error("Lightroom no longer resolves the isolated virtual copy during " .. phase)
    end
    local master = photo:getRawMetadata("masterPhoto")
    if not master or not same_photo(master, context.master_photo) then
        error("isolated virtual copy master changed during " .. phase)
    end
    self:assert_master_identity(master, task, context, phase .. " master check")
    if photo:getFormattedMetadata("copyName") ~= context.preview_copy_name then
        error("isolated virtual copy name changed during " .. phase)
    end
end

function Bridge:assert_working_photo_identity(photo, task, context, phase)
    if context.preview_isolated then
        self:assert_preview_copy_identity(photo, task, context, phase)
    else
        self:assert_photo_identity(photo, task.photo_path, phase)
    end
end

function Bridge:create_preview_virtual_copy(master, task, context)
    local catalog = self:assert_preview_catalog_identity(context, "isolated preview creation")
    self:assert_master_identity(master, task, context, "before isolated preview creation")
    local catalog_path = catalog:getPath()
    if not absolute_windows_path(catalog_path) or has_relative_segment(catalog_path) then
        error("Lightroom catalog path is unavailable for isolated preview recovery")
    end
    local paths = self:preview_copy_record_paths(task.batch_id, task.task_id)
    context.preview_paths = paths
    for _, path in pairs(paths) do
        if LrFileUtils.exists(path) then
            error("preview isolation record already exists; refusing to reuse task identity: " .. path)
        end
    end
    context.selection_before_create = self:capture_selection(catalog, nil)
    local selection_active_uuid, selection_other_uuids = self:selection_record_values(
        context.selection_before_create
    )
    local record = {
        phase = "intent",
        batch_id = task.batch_id,
        task_id = task.task_id,
        catalog_path = catalog_path,
        photo_path = task.photo_path,
        source_uuid = context.source_uuid,
        source_local_id = context.source_local_id,
        selection_active_uuid = selection_active_uuid,
        selection_other_uuids = selection_other_uuids,
        copy_name = preview_copy_name(task.batch_id, task.task_id),
        creation_started = false,
        created_at = now_utc(),
    }
    atomic_create(paths.intent, preview_copy_record_line(record))
    context.preview_record = record
    context.preview_intent_path = paths.intent
    context.preview_copy_name = record.copy_name

    local created = nil
    local operation_ok, operation_error = LrTasks.pcall(function()
        self:activate_preview_library_context(
            context,
            catalog,
            "isolated preview creation selection"
        )
        self:select_only(catalog, master, "isolated preview creation selection")
        self:assert_master_identity(master, task, context, "isolated preview creation selection")
        self:ensure_not_cancelled(task, "before isolated virtual copy creation")
        local started_record = {}
        for key, value in pairs(record) do started_record[key] = value end
        started_record.phase = "started"
        started_record.creation_started = true
        atomic_create(paths.started, preview_copy_record_line(started_record))
        context.preview_record = started_record
        context.preview_started_path = paths.started
        context.preview_creation_started = true
        -- The durable started marker is published first; this selection check
        -- is then the final SDK read before the selection-addressed create.
        self:verify_preview_single_selection(
            context,
            catalog,
            master,
            "immediately before isolated preview creation"
        )
        created = catalog:createVirtualCopies(context.preview_copy_name)
        context.preview_creation_returned = true
        if type(created) ~= "table" or #created ~= 1 then
            error("Lightroom did not create exactly one isolated virtual copy")
        end
        local working = created[1]
        local working_uuid = tostring(working:getRawMetadata("uuid") or "")
        local working_local_id = tonumber(working.localIdentifier)
        if not uuid_identity(working_uuid) or not working_local_id
            or working_local_id < 1 or working_local_id ~= math.floor(working_local_id) then
            error("Lightroom returned an isolated virtual copy without a stable identity")
        end
        context.photo = working
        context.working_uuid = working_uuid
        context.working_local_id = working_local_id
        local active_record = {}
        for key, value in pairs(started_record) do active_record[key] = value end
        active_record.phase = "active"
        active_record.working_uuid = context.working_uuid
        active_record.working_local_id = context.working_local_id
        context.preview_record = active_record
        atomic_create(paths.active, preview_copy_record_line(active_record))
        context.preview_state_path = paths.active
        delete_if_exists(paths.started)
        context.preview_started_path = nil
        delete_if_exists(paths.intent)
        context.preview_intent_path = nil
        -- Persist Lightroom's assigned identity before any semantic assertion.
        -- If a future Lightroom build returns a malformed object, restart
        -- recovery retains the exact UUID record instead of losing the orphan.
        self:assert_preview_copy_identity(working, task, context, "after isolated preview creation")
    end)
    local restore_ok, restore_error = self:restore_saved_preview_selection(
        context,
        catalog,
        "isolated preview creation restore"
    )
    if not restore_ok then context.selection_restore_override = context.selection_before_create end
    if not operation_ok then error(safe_message(operation_error)) end
    if not restore_ok then
        error("could not restore Lightroom selection after isolated preview creation: "
            .. safe_message(restore_error))
    end
    self:assert_preview_copy_identity(context.photo, task, context, "after preview creation selection restore")
    self:ensure_not_cancelled(task, "after isolated virtual copy creation")
    return context.photo
end

function Bridge:remove_preview_virtual_copy(context)
    local task = context.task
    local catalog = self:assert_preview_catalog_identity(context, "isolated preview cleanup")
    context.preview_cleanup_attempted = true
    if not context.working_uuid then
        if context.preview_creation_started then
            local restored, restore_error = self:restore_saved_preview_selection(
                context,
                catalog,
                "identity-unknown preview selection restore"
            )
            local message = "preview_isolation=identity-unknown; orphan_started_record=retained"
            if not restored then message = message .. "; selection_restore_failed=" .. restore_error end
            return false, message
        end
        if context.preview_paths and (
            LrFileUtils.exists(context.preview_paths.started)
            or LrFileUtils.exists(context.preview_paths.active)
        ) then
            return false, "preview_isolation=unexpected-safety-record; orphan_state=retained"
        end
        self:assert_master_identity(
            context.master_photo,
            task,
            context,
            "isolated preview no-create cleanup"
        )
        local restored, restore_error = self:restore_saved_preview_selection(
            context,
            catalog,
            "isolated preview no-create selection restore"
        )
        if not restored then
            return false, "preview_isolation=not-created; selection_restore_failed=" .. restore_error
        end
        self:archive_preview_copy_record(context, context.preview_intent_path)
        context.preview_copy_removed = true
        return true, "preview_isolation=not-created"
    end

    local working = catalog:findPhotoByUuid(context.working_uuid)
    if not working then
        self:assert_master_identity(
            context.master_photo,
            task,
            context,
            "isolated preview already-removed cleanup"
        )
        local restored, restore_error = self:restore_saved_preview_selection(
            context,
            catalog,
            "isolated preview already-removed selection restore"
        )
        if not restored then
            return false, "preview_isolation=already-removed; selection_restore_failed=" .. restore_error
        end
        self:archive_preview_copy_record(
            context,
            context.preview_state_path or context.preview_started_path or context.preview_intent_path
        )
        context.preview_copy_removed = true
        return true, "preview_isolation=already-removed"
    end
    self:assert_preview_copy_identity(working, task, context, "before isolated preview removal")
    local prior_selection = context.selection_restore_override
        or self:capture_selection(catalog, working)
    -- Pin the exact restoration target in the context before any destructive
    -- call so archive/restore failures can retry idempotently without falling
    -- back to the older pre-create selection.
    context.selection_restore_override = prior_selection
    local operation_ok, operation_error = LrTasks.pcall(function()
        self:activate_preview_library_context(
            context,
            catalog,
            "isolated preview removal selection"
        )
        self:select_only(catalog, working, "isolated preview removal selection")
        self:assert_preview_copy_identity(working, task, context, "isolated preview removal selection")
        -- This combined catalog/selection check is deliberately the final SDK
        -- read before the selection-addressed destructive call.
        self:verify_preview_single_selection(
            context,
            catalog,
            working,
            "immediately before isolated preview removal"
        )
        LrSelection.removeFromCatalog()
        self:assert_preview_catalog_identity(context, "after isolated preview removal")
    end)
    local removal_check_ok, removal_check_or_error = LrTasks.pcall(function()
        -- Lightroom can return the just-removed virtual copy briefly while
        -- its catalog UUID index catches up.  The exact UUID remains the only
        -- success condition; bounded retries avoid both false failures and
        -- accidentally accepting a still-live copy.
        for poll = 1, 40 do
            local verified_catalog = self:assert_preview_catalog_identity(
                context,
                "isolated preview removal verification"
            )
            if verified_catalog:findPhotoByUuid(context.working_uuid) == nil then
                self:assert_master_identity(
                    context.master_photo,
                    task,
                    context,
                    "after isolated preview removal"
                )
                return true
            end
            if poll < 40 then LrTasks.sleep(0.05) end
        end
        return false
    end)
    local actually_removed = removal_check_ok and removal_check_or_error == true
    local restore_ok, restore_error = self:restore_preview_library_context(
        context,
        catalog,
        prior_selection,
        "isolated preview removal restore"
    )
    if actually_removed and restore_ok then
        self:archive_preview_copy_record(
            context,
            context.preview_state_path or context.preview_started_path or context.preview_intent_path
        )
        context.preview_copy_removed = true
    end
    if not operation_ok and not actually_removed then
        local message = "preview_isolation=removal-failed; " .. safe_message(operation_error)
        if not removal_check_ok then
            message = message .. "; removal_verification_failed="
                .. safe_message(removal_check_or_error)
        end
        if not restore_ok then message = message .. "; selection_restore_failed=" .. safe_message(restore_error) end
        return false, message
    end
    if not actually_removed then
        local message = "preview_isolation=removal-unverified; orphan_state=retained"
        if not removal_check_ok then
            message = message .. "; removal_verification_failed="
                .. safe_message(removal_check_or_error)
        end
        if not restore_ok then message = message .. "; selection_restore_failed=" .. safe_message(restore_error) end
        return false, message
    end
    if not restore_ok then
        return false, "preview_isolation=removed; selection_restore_failed=" .. safe_message(restore_error)
    end
    if not operation_ok then
        return true, "preview_isolation=removed-after-sdk-error; " .. safe_message(operation_error)
    end
    return true, "preview_isolation=removed"
end

function Bridge:find_or_import(task, context)
    local photo_path = task.photo_path
    self:ensure_not_cancelled(task, "before catalog lookup")
    if not LrFileUtils.exists(photo_path) then error("photo does not exist: " .. photo_path) end
    local catalog = LrApplication.activeCatalog()
    local photo = catalog:findPhotoByPath(photo_path)
    if not photo then
        self:ensure_not_cancelled(task, "before import")
        catalog:withWriteAccessDo("照片选片 · 导入照片", function()
            self:ensure_not_cancelled(task, "inside import")
            photo = catalog:addPhoto(photo_path)
        end)
        context.imported = true
        self:ensure_not_cancelled(task, "after import")
    end
    photo = photo or catalog:findPhotoByPath(photo_path)
    if not photo then error("Lightroom could not import photo: " .. photo_path) end
    self:assert_photo_identity(photo, photo_path, "catalog lookup")
    self:ensure_not_cancelled(task, "after catalog lookup")
    return photo
end

function Bridge:backup_sidecar(task, context)
    self:ensure_not_cancelled(task, "before XMP backup")
    local stem = task.batch_id .. "--" .. task.task_id
    local state_path = LrPathUtils.child(self.paths.backups, stem .. ".xmp.state")
    local backup_path = LrPathUtils.child(self.paths.backups, stem .. ".xmp.original")
    local signature = sidecar_signature(context.sidecar_path)
    local backup_value = ""
    if signature.exists then
        local contents = read_all(context.sidecar_path, MAX_XMP_BACKUP_BYTES)
        atomic_create(backup_path, contents)
        backup_value = backup_path
        context.pre_sidecar_contents = contents
    end
    local state = table.concat({
        XMP_BACKUP_PROTOCOL,
        "batch_id=" .. encode_scalar(task.batch_id),
        "task_id=" .. encode_scalar(task.task_id),
        "photo_path=" .. encode_scalar(task.photo_path),
        "xmp_path=" .. encode_scalar(context.sidecar_path),
        "existed=" .. encode_scalar(signature.exists),
        "size=" .. encode_scalar(signature.size or 0),
        "modified=" .. encode_scalar(signature.modified or ""),
        "backup_path=" .. encode_scalar(backup_value),
        "created_at=" .. encode_scalar(now_utc()),
    }, "\t") .. "\n"
    atomic_create(state_path, state)
    context.pre_sidecar = signature
    context.xmp_backup_path = signature.exists and backup_path or nil
    context.xmp_state_path = state_path
    self:ensure_not_cancelled(task, "after XMP backup")
end

function Bridge:create_snapshot(photo, task, context)
    self:ensure_not_cancelled(task, "before snapshot")
    self:assert_photo_identity(photo, task.photo_path, "before snapshot")
    local snapshot_name = table.concat({
        SNAPSHOT_PREFIX,
        os.date("!%Y%m%d-%H%M%S"),
        task.batch_id .. "--" .. task.task_id,
    }, " · ")
    context.snapshot_name = snapshot_name
    local catalog = LrApplication.activeCatalog()
    local created = false
    catalog:withWriteAccessDo("照片选片 · 建立处理前快照", function()
        self:ensure_not_cancelled(task, "inside snapshot")
        self:assert_photo_identity(photo, task.photo_path, "inside snapshot")
        created = photo:createDevelopSnapshot(snapshot_name, false)
    end)
    if not created then error("Lightroom refused the unique pre-edit snapshot") end

    local snapshot_id = nil
    for _, snapshot in ipairs(photo:getDevelopSnapshots() or {}) do
        if snapshot.name == snapshot_name then snapshot_id = snapshot.snapshotID end
    end
    if not snapshot_id then error("Lightroom did not return the new snapshot ID") end
    context.snapshot_id = snapshot_id
    self:assert_photo_identity(photo, task.photo_path, "after snapshot")
    self:ensure_not_cancelled(task, "after snapshot")
end

local function bridge_snapshot_identity(snapshot)
    local name = snapshot and snapshot.name
    local delimiter = " · "
    local head = SNAPSHOT_PREFIX .. delimiter
    if type(name) ~= "string" or name:sub(1, #head) ~= head then return nil end
    local separator = name:find(delimiter, #head + 1, true)
    if not separator then return nil end
    local timestamp = name:sub(#head + 1, separator - 1)
    if not timestamp:match("^%d%d%d%d%d%d%d%d%-%d%d%d%d%d%d$") then return nil end
    local identity = name:sub(separator + #delimiter)
    if identity == "" or identity:find(delimiter, 1, true) then return nil end
    local boundary = identity:find("--", 1, true)
    if not boundary then return nil end
    local batch_id = identity:sub(1, boundary - 1)
    local task_id = identity:sub(boundary + 2)
    if not valid_identifier(batch_id) or not valid_identifier(task_id) then return nil end
    return {
        identity = identity,
        batch_id = batch_id,
        task_id = task_id,
        timestamp = timestamp,
    }
end

local function style_preview_snapshot_identity(snapshot)
    local parsed = bridge_snapshot_identity(snapshot)
    if not parsed or parsed.batch_id:sub(1, 6) ~= "style-" then return nil end
    return parsed.identity
end

function Bridge:delete_style_preview_snapshots(photo, task, context)
    self:ensure_not_cancelled(task, "before style preview snapshot repair")
    self:assert_photo_identity(photo, task.photo_path, "before style preview snapshot repair")
    local targets = {}
    for _, snapshot in ipairs(photo:getDevelopSnapshots() or {}) do
        local identity = style_preview_snapshot_identity(snapshot)
        if identity then
            local delete_id = snapshot.id_global or snapshot.snapshotID
            if not delete_id then
                error("matching style preview snapshot has no Lightroom snapshot ID: " .. identity)
            end
            table.insert(targets, { id = delete_id, identity = identity })
        end
    end
    if #targets == 0 then return 0 end

    local catalog = LrApplication.activeCatalog()
    catalog:withWriteAccessDo("照片选片 · 清理遗留风格预览快照", function()
        for _, target in ipairs(targets) do
            self:ensure_not_cancelled(task, "inside style preview snapshot repair")
            self:assert_photo_identity(photo, task.photo_path, "inside style preview snapshot repair")
            photo:deleteDevelopSnapshot(target.id)
            context.result.cleanup_count = context.result.cleanup_count + 1
        end
    end)
    self:assert_photo_identity(photo, task.photo_path, "after style preview snapshot repair")
    for _, snapshot in ipairs(photo:getDevelopSnapshots() or {}) do
        if style_preview_snapshot_identity(snapshot) then
            error("a matching style preview snapshot remains after metadata repair")
        end
    end
    self:ensure_not_cancelled(task, "after style preview snapshot repair")
    return context.result.cleanup_count
end

function Bridge:wait_for_sidecar_change(path, before, before_contents, task, ignore_cancel, photo)
    local stable_contents = nil
    local stable_count = 0
    local last_error = nil
    for poll = 1, SIDECAR_TIMEOUT_POLLS do
        if task and not ignore_cancel then
            self:ensure_not_cancelled(task, "while waiting for XMP")
        end
        local ok, current, contents = pcall(function()
            local signature = sidecar_signature(path)
            local bytes = nil
            if signature.exists and signature.size and signature.size > 0 then
                bytes = read_all(path, MAX_XMP_BACKUP_BYTES)
            end
            return signature, bytes
        end)
        if ok then
            local contents_changed = contents ~= nil
                and ((not before.exists) or contents ~= before_contents)
            -- Exact byte comparison is stronger than relying on filesystem
            -- timestamp granularity or a same-size XMP rewrite.
            if contents_changed then
                if contents == stable_contents then
                    stable_count = stable_count + 1
                else
                    stable_contents = contents
                    stable_count = 1
                end
                if stable_count >= SIDECAR_STABLE_POLLS then return current, contents end
            else
                stable_contents = nil
                stable_count = 0
            end
        else
            last_error = current
            stable_contents = nil
            stable_count = 0
        end
        if photo and poll < SIDECAR_TIMEOUT_POLLS and poll % SIDECAR_SAVE_RETRY_POLLS == 0 then
            local retry_ok, retry_error = LrTasks.pcall(function() photo:saveMetadata() end)
            if not retry_ok then last_error = "saveMetadata retry: " .. safe_message(retry_error) end
        end
        LrTasks.sleep(SIDECAR_POLL_SECONDS)
    end
    local detail = last_error and (" (last attribute error: " .. safe_message(last_error) .. ")") or ""
    error("Lightroom did not update and stabilize the expected XMP within 20 minutes: " .. path .. detail)
end

function Bridge:wait_for_repaired_metadata(photo, task, context)
    local stable_contents = nil
    local stable_count = 0
    local last_error = nil
    local last_status = "unknown"
    for poll = 1, SIDECAR_TIMEOUT_POLLS do
        self:ensure_not_cancelled(task, "while waiting for repaired XMP")
        local ok, current, contents, metadata_status = pcall(function()
            local signature = sidecar_signature(context.sidecar_path)
            local bytes = nil
            if signature.exists and signature.size and signature.size > 0 then
                bytes = read_all(context.sidecar_path, MAX_XMP_BACKUP_BYTES)
            end
            return signature, bytes, photo:getRawMetadata("metadataStatus")
        end)
        if ok then
            last_status = tostring(metadata_status or "unknown")
            local contents_changed = contents ~= nil
                and ((not context.pre_sidecar.exists) or contents ~= context.pre_sidecar_contents)
            local save_is_observable = contents_changed or metadata_status == "upToDate"
            if current.exists and contents and save_is_observable then
                if contents == stable_contents then
                    stable_count = stable_count + 1
                else
                    stable_contents = contents
                    stable_count = 1
                end
                if stable_count >= SIDECAR_STABLE_POLLS then return current, contents end
            else
                stable_contents = nil
                stable_count = 0
            end
        else
            last_error = current
            stable_contents = nil
            stable_count = 0
        end
        if poll < SIDECAR_TIMEOUT_POLLS and poll % SIDECAR_SAVE_RETRY_POLLS == 0 then
            local retry_ok, retry_error = LrTasks.pcall(function() photo:saveMetadata() end)
            if not retry_ok then last_error = "saveMetadata retry: " .. safe_message(retry_error) end
        end
        LrTasks.sleep(SIDECAR_POLL_SECONDS)
    end
    local detail = "; metadataStatus=" .. last_status
    if last_error then detail = detail .. "; last_error=" .. safe_message(last_error) end
    error("Lightroom did not save and stabilize the repaired XMP within 20 minutes: "
        .. context.sidecar_path .. detail)
end

function Bridge:repair_preview_metadata(task, context)
    context.sidecar_path = LrPathUtils.replaceExtension(task.photo_path, "xmp")
    context.result.cleanup_count = 0
    context.result.xmp_status = "running"

    -- The immutable sidecar copy is the safety boundary for this deliberately
    -- narrow repair. It must exist before any catalog snapshot is deleted.
    self:backup_sidecar(task, context)
    local photo = self:find_existing_photo(task)
    context.photo = photo
    self:delete_style_preview_snapshots(photo, task, context)

    self:ensure_not_cancelled(task, "before repaired XMP save")
    self:assert_photo_identity(photo, task.photo_path, "before repaired XMP save")
    local saved, save_error = LrTasks.pcall(function() photo:saveMetadata() end)
    if not saved then error("repair saveMetadata failed: " .. safe_message(save_error)) end
    context.final_sidecar, context.final_sidecar_contents = self:wait_for_repaired_metadata(
        photo,
        task,
        context
    )
    self:assert_photo_identity(photo, task.photo_path, "after repaired XMP save")
    self:ensure_not_cancelled(task, "after repaired XMP save")
    context.result.xmp_status = "done"
    context.result.xmp_path = context.sidecar_path
    context.xmp_committed = true
    context.xmp_verified = true
end

function Bridge:wait_for_exact_sidecar(path, expected)
    local stable_key = nil
    local stable_count = 0
    for _ = 1, 40 do
        local ok, contents = pcall(function() return read_all(path, MAX_XMP_BACKUP_BYTES) end)
        if ok and contents == expected then
            local signature = sidecar_signature(path)
            local key = signature_key(signature)
            if key == stable_key then
                stable_count = stable_count + 1
            else
                stable_key = key
                stable_count = 1
            end
            if stable_count >= SIDECAR_STABLE_POLLS then return end
        else
            stable_key = nil
            stable_count = 0
        end
        LrTasks.sleep(SIDECAR_POLL_SECONDS)
    end
    error("restored XMP did not remain byte-identical and stable: " .. path)
end

function Bridge:restore_sidecar_backup(context)
    local original = read_all(context.xmp_backup_path, MAX_XMP_BACKUP_BYTES)
    local temporary = context.sidecar_path .. ".photo-ai-restore.tmp"
    if LrFileUtils.exists(temporary) then
        local removed = LrFileUtils.delete(temporary)
        if not removed then error("cannot clear stale XMP restore temporary file") end
    end
    local copied, copy_error = LrFileUtils.copy(context.xmp_backup_path, temporary)
    if not copied then error(copy_error or "cannot stage the XMP backup for restore") end
    if read_all(temporary, MAX_XMP_BACKUP_BYTES) ~= original then
        error("staged XMP restore copy differs from the immutable backup")
    end

    local displaced = LrPathUtils.child(
        self.paths.backups,
        context.task.batch_id .. "--" .. context.task.task_id .. ".xmp.failed-current"
    )
    if LrFileUtils.exists(context.sidecar_path) then
        if LrFileUtils.exists(displaced) then
            error("refusing to overwrite the prior displaced XMP safety copy")
        end
        local moved_current, move_current_error = LrFileUtils.move(context.sidecar_path, displaced)
        if not moved_current then
            error(move_current_error or "cannot preserve the current XMP before restore")
        end
        context.rollback_displaced_xmp_path = displaced
    end

    local moved_restore, move_restore_error = LrFileUtils.move(temporary, context.sidecar_path)
    if not moved_restore then
        if context.rollback_displaced_xmp_path and not LrFileUtils.exists(context.sidecar_path) then
            pcall(function() LrFileUtils.move(context.rollback_displaced_xmp_path, context.sidecar_path) end)
        end
        error(move_restore_error or "cannot publish the restored XMP")
    end
    self:wait_for_exact_sidecar(context.sidecar_path, original)
end

function Bridge:sidecar_is_unchanged(context)
    if not context.pre_sidecar_contents or not LrFileUtils.exists(context.sidecar_path) then
        return false
    end
    local current = read_all(context.sidecar_path, MAX_XMP_BACKUP_BYTES)
    if current ~= context.pre_sidecar_contents then return false end
    -- Do not replace an unchanged sidecar merely to prove that it is unchanged.
    -- Keeping the same file identity and timestamp prevents Lightroom from
    -- interpreting a transient preview rollback as an external metadata edit.
    self:wait_for_exact_sidecar(context.sidecar_path, context.pre_sidecar_contents)
    return true
end

function Bridge:delete_transient_snapshot(photo, context)
    if not context.transient or not context.snapshot_id then return true, nil end
    local catalog = LrApplication.activeCatalog()
    local target = nil
    local name_collision = false
    local id_collision = false
    for _, snapshot in ipairs(photo:getDevelopSnapshots() or {}) do
        if snapshot.name == context.snapshot_name then name_collision = true end
        if snapshot.snapshotID == context.snapshot_id then id_collision = true end
        if snapshot.name == context.snapshot_name and snapshot.snapshotID == context.snapshot_id then
            target = snapshot
        end
    end
    if not target then
        if name_collision or id_collision then
            return false, "temporary Lightroom snapshot identity changed before cleanup"
        end
        context.snapshot_id = nil
        context.snapshot_name = nil
        return true, nil
    end

    local prior_module = LrApplicationView.getCurrentModuleName()
    if type(prior_module) ~= "string" or prior_module == "" then
        return false, "Lightroom current module is unavailable before snapshot cleanup"
    end

    local function switch_module(module, phase)
        if LrApplicationView.getCurrentModuleName() ~= module then
            LrApplicationView.switchToModule(module)
        end
        for poll = 1, 100 do
            if LrApplicationView.getCurrentModuleName() == module then return end
            if poll < 100 then LrTasks.sleep(0.05) end
        end
        error("Lightroom module did not stabilize during " .. phase)
    end

    local function delete_snapshot(delete_id, phase)
        local operation_ok, operation_error = LrTasks.pcall(function()
            switch_module("develop", phase .. " develop activation")
            self:assert_photo_identity(photo, context.task.photo_path, phase)
            catalog:withWriteAccessDo("照片选片 · 删除临时处理快照", function()
                self:assert_photo_identity(photo, context.task.photo_path, phase .. " write gate")
                photo:deleteDevelopSnapshot(delete_id)
            end)
        end)
        local restore_ok, restore_error = LrTasks.pcall(function()
            switch_module(prior_module, phase .. " module restore")
        end)
        if not restore_ok then
            return false, "Lightroom module restore failed: " .. safe_message(restore_error)
        end
        if not operation_ok then return false, operation_error end
        return true, nil
    end

    local function still_present()
        for _, snapshot in ipairs(photo:getDevelopSnapshots() or {}) do
            if snapshot.name == context.snapshot_name and snapshot.snapshotID == context.snapshot_id then
                return true
            end
        end
        return false
    end

    -- Lightroom documents an opaque snapshot ID, but its returned snapshot
    -- table distinguishes snapshotID (apply) from id_global (delete). Deletion
    -- is also effective only while Lightroom is in Develop, so briefly switch
    -- modules and always restore the user's prior module before returning.
    local primary_id = target.id_global or target.snapshotID
    local deleted, delete_error = delete_snapshot(primary_id, "temporary snapshot cleanup")
    if not deleted and target.snapshotID and target.snapshotID ~= primary_id then
        deleted, delete_error = delete_snapshot(
            target.snapshotID,
            "temporary snapshot cleanup compatibility fallback"
        )
    end
    if not deleted then return false, safe_message(delete_error) end

    for poll = 1, 100 do
        if not still_present() then
            context.snapshot_id = nil
            context.snapshot_name = nil
            return true, nil
        end
        if poll < 100 then LrTasks.sleep(0.05) end
    end

    -- Some SDK builds accept the local ID but defer or ignore the deletion.
    -- Retry only the same exact-matched record with its alternate identifier.
    if target.snapshotID and target.snapshotID ~= primary_id then
        local fallback_ok, fallback_error = delete_snapshot(
            target.snapshotID,
            "temporary snapshot cleanup delayed compatibility fallback"
        )
        if not fallback_ok then return false, safe_message(fallback_error) end
        for poll = 1, 100 do
            if not still_present() then
                context.snapshot_id = nil
                context.snapshot_name = nil
                return true, nil
            end
            if poll < 100 then LrTasks.sleep(0.05) end
        end
    end

    return false, "temporary Lightroom snapshot still exists after bounded cleanup wait"
end

function Bridge:cleanup_transient_snapshot(task, context)
    self:ensure_not_cancelled(task, "before exact transient snapshot cleanup")
    local photo = self:find_existing_photo(task)
    context.photo = photo
    context.result.cleanup_count = 0
    local targets = {}
    for _, snapshot in ipairs(photo:getDevelopSnapshots() or {}) do
        local identity = bridge_snapshot_identity(snapshot)
        if identity and identity.batch_id == task.source_batch_id
            and identity.task_id == task.source_task_id then
            table.insert(targets, snapshot)
        end
    end
    if #targets == 0 then return end
    if #targets ~= 1 then
        error("exact transient snapshot cleanup found more than one matching snapshot")
    end
    local target = targets[1]
    if not target.snapshotID or not target.name then
        error("exact transient snapshot cleanup target has no stable local identity")
    end
    context.transient = true
    context.snapshot_id = target.snapshotID
    context.snapshot_name = target.name
    local deleted, delete_error = self:delete_transient_snapshot(photo, context)
    if not deleted then error("exact transient snapshot cleanup failed: " .. delete_error) end
    context.result.cleanup_count = 1
    self:assert_photo_identity(photo, task.photo_path, "after exact transient snapshot cleanup")
    self:ensure_not_cancelled(task, "after exact transient snapshot cleanup")
end

function Bridge:resolve_preset(task)
    if not task.preset_uuid then return nil end
    local preset = nil
    if task.preset_scope == "plugin" then
        self:register_managed_presets(false)
        preset = LrApplication.getDevelopPresetsForPlugin(_PLUGIN, task.preset_uuid)
    else
        preset = LrApplication.developPresetByUuid(task.preset_uuid)
    end
    if not preset then error("Lightroom preset UUID was not found: " .. task.preset_uuid) end
    return preset
end

function Bridge:load_look_descriptor(task)
    if not task.look_descriptor_hash then return nil end
    if not self.paths or type(self.paths.looks) ~= "string" then
        error("Look descriptor root is not configured")
    end
    local expected_leaf = task.look_descriptor_hash:lower() .. ".look"
    local descriptor_path = LrPathUtils.child(self.paths.looks, expected_leaf)
    if path_key(LrPathUtils.parent(descriptor_path)) ~= path_key(self.paths.looks)
        or path_key(LrPathUtils.leafName(descriptor_path)) ~= expected_leaf then
        error("Look descriptor filename does not match its SHA-256")
    end
    local contents = read_all(descriptor_path, MAX_LOOK_DESCRIPTOR_BYTES)
    local actual_hash = LrDigest.SHA256.digest(contents)
    if type(actual_hash) ~= "string" or actual_hash:lower() ~= task.look_descriptor_hash:lower() then
        error("Look descriptor SHA-256 mismatch")
    end
    local look = parse_look_descriptor(contents, task.look_uuid)
    if task.look_amount ~= 100 and look.SupportsAmount ~= true then
        error("Creative Look does not support a custom Amount")
    end
    look.Amount = task.look_amount / 100
    return look
end

function Bridge:verify_applied_look(photo, task)
    local settings = photo:getDevelopSettings()
    local actual = settings and settings.Look
    if type(actual) ~= "table" or actual.UUID ~= task.look_uuid then
        error("Creative Look verification failed for UUID: expected "
            .. tostring(task.look_uuid) .. ", got "
            .. tostring(type(actual) == "table" and actual.UUID or nil))
    end
    local expected_amount = task.look_amount / 100
    local actual_amount = tonumber(actual.Amount)
    if not actual_amount or math.abs(actual_amount - expected_amount) > 0.0001 then
        error("Creative Look verification failed for Amount: expected "
            .. tostring(expected_amount) .. ", got " .. tostring(actual_amount))
    end
    return actual
end

function Bridge:apply_develop_settings(photo, task, context)
    local settings = {}
    if task.auto_tone then settings.AutoTone = true end
    if task.auto_white_balance then settings.WhiteBalance = "Auto" end
    if task.lens_profile then settings.LensProfileEnable = 1 end
    if task.remove_ca then settings.AutoLateralCA = 1 end
    local has_crop = false
    for key, value in pairs(task.crop) do
        settings[key] = value
        has_crop = true
    end
    if has_crop then settings.HasCrop = true end
    for key, value in pairs(task.style) do settings[key] = value end

    local look = self:load_look_descriptor(task)
    self:ensure_not_cancelled(task, "before object-level develop settings")
    self:assert_working_photo_identity(photo, task, context, "before object-level develop settings")
    context.modified = true
    local catalog = LrApplication.activeCatalog()
    catalog:withWriteAccessDo("照片选片 · 应用自动调整与裁切", function()
        self:ensure_not_cancelled(task, "inside object-level develop settings")
        self:assert_working_photo_identity(photo, task, context, "inside object-level develop settings")
        -- A preview is a transient develop render, not a metadata operation.
        -- Lightroom rejects setRawMetadata("rating", 0) even though 0 is our
        -- wire representation for "unrated".  More importantly, previews must
        -- never touch the user's rating, so leave it unchanged here.
        if task.task_type ~= "preview" then
            photo:setRawMetadata("rating", task.rating)
        end
        if task.preset_uuid then
            context.result.preset_status = "running"
            local preset = self:resolve_preset(task)
            if task.preset_scope == "plugin" then
                photo:applyDevelopPreset(preset, _PLUGIN, task.preset_amount, true)
            else
                photo:applyDevelopPreset(preset, nil, task.preset_amount, true)
            end
            context.result.preset_status = "done"
        end
        if has_items(settings) then
            -- optFlattenAutoNow=true resolves AutoTone synchronously for this
            -- exact photo object; no Develop UI target or active source is used.
            photo:applyDevelopSettings(settings, "照片选片 · 自动调整", true)
        end
        if look then
            context.result.look_status = "running"
            photo:applyDevelopSettings({ Look = look }, "照片选片 · 创意外观", true)
        end
    end)
    self:assert_working_photo_identity(photo, task, context, "after object-level develop settings")
    if look then
        self:verify_applied_look(photo, task)
        context.result.look_status = "done"
        context.result.look_uuid = task.look_uuid
        context.result.look_amount = task.look_amount
    end
    self:ensure_not_cancelled(task, "after object-level develop settings")
end

function Bridge:save_xmp(photo, task, context)
    self:ensure_not_cancelled(task, "before saveMetadata")
    local saved, save_error = LrTasks.pcall(function() photo:saveMetadata() end)
    if not saved then error("saveMetadata failed: " .. safe_message(save_error)) end
    self:ensure_not_cancelled(task, "after saveMetadata")
    context.final_sidecar, context.final_sidecar_contents = self:wait_for_sidecar_change(
        context.sidecar_path,
        context.pre_sidecar,
        context.pre_sidecar_contents,
        task,
        false,
        photo
    )
    require_xmp_number(context.final_sidecar_contents, "xmp:Rating", task.rating, 0)
    local left, top = task.crop.CropLeft, task.crop.CropTop
    local right, bottom = task.crop.CropRight, task.crop.CropBottom
    local nontrivial_crop = left ~= nil and (
        math.abs(left) > 0.000001 or math.abs(top) > 0.000001
        or math.abs(right - 1) > 0.000001 or math.abs(bottom - 1) > 0.000001
    )
    if nontrivial_crop then
        require_xmp_number(context.final_sidecar_contents, "crs:CropLeft", left, 0.00001)
        require_xmp_number(context.final_sidecar_contents, "crs:CropTop", top, 0.00001)
        require_xmp_number(context.final_sidecar_contents, "crs:CropRight", right, 0.00001)
        require_xmp_number(context.final_sidecar_contents, "crs:CropBottom", bottom, 0.00001)
    end
    if task.crop.CropAngle and math.abs(task.crop.CropAngle) > 0.000001 then
        require_xmp_number(context.final_sidecar_contents, "crs:CropAngle", task.crop.CropAngle, 0.0001)
    end
    if task.style.CameraProfile then
        require_xmp_string(context.final_sidecar_contents, "crs:CameraProfile", task.style.CameraProfile)
        require_xmp_profile_amount(context.final_sidecar_contents, task.style.ProfileAmount)
    end
    self:assert_photo_identity(photo, task.photo_path, "after XMP write")
    self:ensure_not_cancelled(task, "after XMP write")
    context.xmp_verified = true
end

-- Retained as the legacy operation boundary for protocol /1 callers.
function Bridge:apply_settings(photo, task, context)
    self:apply_develop_settings(photo, task, context)
    self:save_xmp(photo, task, context)
end

function Bridge:export_jpeg(photo, task, context, preview)
    self:ensure_not_cancelled(task, "before JPEG export")
    self:assert_working_photo_identity(photo, task, context, "before JPEG export")
    local destination = task.jpeg_output_dir
    if not destination or destination == "" then
        if preview then
            destination = self.paths.previews
        else
            destination = LrPathUtils.child(LrPathUtils.parent(task.photo_path), "成片")
        end
    end
    LrFileUtils.createAllDirectories(destination)
    local export_settings = {
        LR_exportServiceProvider = "com.adobe.ag.export.file",
        LR_export_destinationType = "specificFolder",
        LR_export_destinationPathPrefix = destination,
        LR_export_useSubfolder = false,
        LR_collisionHandling = "rename",
        LR_format = "JPEG",
        LR_jpeg_quality = 0.9,
        LR_export_colorSpace = "sRGB",
        LR_size_doConstrain = false,
        LR_outputSharpeningOn = true,
        LR_outputSharpeningMedia = "screen",
        LR_outputSharpeningLevel = 2,
        LR_reimportExportedPhoto = false,
        LR_useWatermark = false,
        LR_jpeg_useLimitSize = false,
    }
    if preview then
        export_settings.LR_size_doConstrain = true
        export_settings.LR_size_doNotEnlarge = true
        export_settings.LR_size_resizeType = "wh"
        export_settings.LR_size_maxWidth = 1024
        export_settings.LR_size_maxHeight = 1024
        export_settings.LR_size_units = "pixels"
    end
    local session = LrExportSession {
        photosToExport = { photo },
        exportSettings = export_settings,
    }
    local rendered_path = nil
    for _, rendition in session:renditions({ stopIfCanceled = true }) do
        self:ensure_not_cancelled(task, "before JPEG rendition")
        local success, path_or_message = rendition:waitForRender()
        self:ensure_not_cancelled(task, "after JPEG rendition")
        if not success then error("JPEG export failed: " .. safe_message(path_or_message)) end
        rendered_path = path_or_message
    end
    if not rendered_path then error("JPEG export produced no rendition") end
    self:assert_working_photo_identity(photo, task, context, "after JPEG export")
    return rendered_path
end

local function preset_value(preset, method, fallback)
    local ok, value = pcall(function() return preset[method](preset) end)
    if ok and value ~= nil then return tostring(value) end
    return fallback or ""
end

local function plugin_presets_by_name()
    local records = {}
    for _, preset in ipairs(LrApplication.getDevelopPresetsForPlugin(_PLUGIN) or {}) do
        local name = preset_value(preset, "getName")
        local uuid = preset_value(preset, "getUuid")
        if name ~= "" and valid_identifier(uuid) then
            local current = records[name]
            if not current or uuid < current.uuid then
                records[name] = { preset = preset, uuid = uuid }
            end
        end
    end
    return records
end

local function registration_json(registry_hash, records)
    local registered = 0
    local failed = 0
    local rows = {}
    for _, record in ipairs(records) do
        if record.status == "registered" then registered = registered + 1 else failed = failed + 1 end
        local values = {
            '      "preset_id": ' .. json_string(record.preset_id),
            '      "file_hash": ' .. json_string(record.file_hash),
            '      "plugin_name": ' .. json_string(record.plugin_name),
            '      "plugin_uuid": ' .. (record.plugin_uuid and json_string(record.plugin_uuid) or "null"),
            '      "scope": "plugin"',
            '      "status": ' .. json_string(record.status),
            '      "message": ' .. json_string(record.message or ""),
        }
        table.insert(rows, "    {\n" .. table.concat(values, ",\n") .. "\n    }")
    end
    local status = failed == 0 and "complete" or "partial"
    return table.concat({
        "{",
        '  "schema_version": 1,',
        '  "registry_hash": ' .. json_string(registry_hash) .. ",",
        '  "generated_at": ' .. json_string(now_utc()) .. ",",
        '  "status": ' .. json_string(status) .. ",",
        '  "registered_count": ' .. tostring(registered) .. ",",
        '  "failed_count": ' .. tostring(failed) .. ",",
        '  "entries": [',
        table.concat(rows, ",\n"),
        "  ]",
        "}",
        "",
    }, "\n"), { status = status, registered = registered, failed = failed, total = #records }
end

function Bridge:publish_managed_registration(registry_hash, records)
    if not self.managed_paths then error("managed preset registry path is not safely configured") end
    local contents, summary = registration_json(registry_hash, records)
    atomic_write(self.managed_paths.registration, contents)
    self.managed_registration_entries = records
    self.managed_registration_summary = summary
    return summary
end

function Bridge:register_managed_presets(force)
    if not self.managed_paths then return { status = "unconfigured", registered = 0, failed = 0, total = 0 } end
    if not LrFileUtils.exists(self.managed_paths.registry) then
        self.managed_registry_signature = nil
        return { status = "missing", registered = 0, failed = 0, total = 0 }
    end
    local signature = file_signature(self.managed_paths.registry)
    if not force and signature == self.managed_registry_signature
        and self.managed_registration_entries and self.managed_registration_summary then
        if not LrFileUtils.exists(self.managed_paths.registration) then
            self:publish_managed_registration(
                self.managed_registry_hash,
                self.managed_registration_entries
            )
        end
        return self.managed_registration_summary
    end

    local registry = load_managed_registry(self.managed_paths.registry)
    if not force and registry.registry_hash == self.managed_registry_hash
        and self.managed_registration_entries and self.managed_registration_summary then
        -- Generating the same logical registry can update its timestamp. Reuse
        -- the already verified Lightroom UUID mapping when the content hash is
        -- unchanged, while recreating the state file if it was removed.
        self.managed_registry_signature = signature
        if not LrFileUtils.exists(self.managed_paths.registration) then
            self:publish_managed_registration(
                self.managed_registry_hash,
                self.managed_registration_entries
            )
        end
        return self.managed_registration_summary
    end

    local existing = plugin_presets_by_name()
    local add_errors = {}
    for _, entry in ipairs(registry.entries) do
        if not existing[entry.plugin_name] then
            local added, preset_or_error = LrTasks.pcall(function()
                return LrApplication.addDevelopPresetForPlugin(
                    _PLUGIN,
                    entry.plugin_name,
                    entry.develop_settings
                )
            end)
            if added and preset_or_error then
                local uuid = preset_value(preset_or_error, "getUuid")
                if valid_identifier(uuid) then
                    existing[entry.plugin_name] = { preset = preset_or_error, uuid = uuid }
                end
            elseif not added then
                add_errors[entry.plugin_name] = safe_message(preset_or_error)
            end
        end
    end

    -- Some SDK builds return nil even when addDevelopPresetForPlugin succeeds.
    -- Re-enumeration is the authority for the UUID written back to Python.
    existing = plugin_presets_by_name()
    local records = {}
    for _, entry in ipairs(registry.entries) do
        local found = existing[entry.plugin_name]
        table.insert(records, {
            preset_id = entry.preset_id,
            file_hash = entry.file_hash,
            plugin_name = entry.plugin_name,
            plugin_uuid = found and found.uuid or nil,
            status = found and "registered" or "failed",
            message = found and "" or (add_errors[entry.plugin_name] or "Lightroom did not enumerate the hidden preset"),
        })
    end
    self.managed_registry_hash = registry.registry_hash
    self.managed_registry_signature = signature
    local summary = self:publish_managed_registration(registry.registry_hash, records)
    self:log(
        summary.failed == 0 and "INFO" or "ERROR",
        "managed presets: registered=" .. tostring(summary.registered)
            .. ", failed=" .. tostring(summary.failed)
            .. ", registry=" .. registry.registry_hash
    )
    return summary
end

function Bridge:refresh_managed_presets_if_due()
    local now = os.time()
    if now < (self.next_managed_registry_check or 0) then return end
    self.next_managed_registry_check = now + 10
    self:register_managed_presets(false)
end

function Bridge:enumerate_presets(task, running_path)
    self:ensure_not_cancelled(task, "before preset enumeration")
    self:register_managed_presets(false)
    local records = {}
    local function add(scope, preset, folder_name)
        local uuid = preset_value(preset, "getUuid")
        if uuid == "" or not valid_identifier(uuid) then return end
        table.insert(records, {
            scope = scope,
            uuid = uuid,
            name = preset_value(preset, "getName"),
            folder = folder_name or "",
            file = preset_value(preset, "getFile"),
        })
    end
    for _, folder in ipairs(LrApplication.developPresetFolders() or {}) do
        local folder_name = preset_value(folder, "getName")
        local ok, presets = pcall(function() return folder:getDevelopPresets() end)
        if ok then
            for _, preset in ipairs(presets or {}) do add("catalog", preset, folder_name) end
        end
    end
    for _, preset in ipairs(LrApplication.getDevelopPresetsForPlugin(_PLUGIN) or {}) do
        add("plugin", preset, "照片选片（隐藏）")
    end
    table.sort(records, function(left, right)
        local left_key = left.scope .. "\0" .. left.name .. "\0" .. left.uuid
        local right_key = right.scope .. "\0" .. right.name .. "\0" .. right.uuid
        return left_key < right_key
    end)
    local lines = {}
    for _, record in ipairs(records) do
        table.insert(lines, table.concat({
            PRESET_PROTOCOL,
            "scope=" .. encode_scalar(record.scope),
            "uuid=" .. encode_scalar(record.uuid),
            "name=" .. encode_scalar(record.name),
            "folder=" .. encode_scalar(record.folder),
            "file=" .. encode_scalar(record.file),
        }, "\t"))
    end
    local listing_path = LrPathUtils.child(self.paths.presets, task_name(running_path) .. ".presets")
    atomic_write(listing_path, (#lines > 0 and table.concat(lines, "\n") .. "\n" or ""))
    self:ensure_not_cancelled(task, "after preset enumeration")
    return listing_path, #records
end

function Bridge:parse_task(path)
    local fields = parse_line(read_first_line(path), TASK_PROTOCOL)
    local required = { "batch_id", "task_id", "photo_path", "rating", "auto_tone", "auto_white_balance", "lens_profile", "remove_ca" }
    for _, key in ipairs(required) do
        if fields[key] == nil then error("missing field: " .. key) end
    end
    local legacy_look_descriptor_path = fields.look_descriptor_path
    if legacy_look_descriptor_path ~= nil
        and type(legacy_look_descriptor_path) ~= "string" then
        error("legacy look_descriptor_path must be a string")
    end
    local task = {
        batch_id = fields.batch_id,
        task_id = fields.task_id,
        photo_path = fields.photo_path,
        rating = fields.rating,
        auto_tone = fields.auto_tone,
        auto_white_balance = fields.auto_white_balance,
        lens_profile = fields.lens_profile,
        remove_ca = fields.remove_ca,
        task_type = fields.task_type or "apply",
        output_mode = fields.output_mode,
        preset_uuid = fields.preset_uuid,
        preset_scope = fields.preset_scope or "catalog",
        preset_amount = fields.preset_amount or 100,
        look_descriptor_hash = fields.look_descriptor_hash,
        look_uuid = fields.look_uuid,
        look_amount = fields.look_amount or 100,
        jpeg_output_dir = fields.jpeg_output_dir,
        source_batch_id = fields.source_batch_id,
        source_task_id = fields.source_task_id,
        crop = {},
        style = {},
    }
    if not task.output_mode then
        task.output_mode = task.task_type == "preview" and "jpeg" or "xmp"
    end
    if not valid_identifier(task.batch_id) then error("invalid batch_id") end
    if not valid_identifier(task.task_id) then error("invalid task_id") end
    if task.task_type ~= "apply" and task.task_type ~= "preview"
        and task.task_type ~= "enumerate_presets"
        and task.task_type ~= "repair_preview_metadata"
        and task.task_type ~= "cleanup_transient_snapshot" then
        error("unsupported task_type")
    end
    if task.output_mode ~= "xmp" and task.output_mode ~= "jpeg" and task.output_mode ~= "both" then
        error("unsupported output_mode")
    end
    if task.task_type == "preview" and task.output_mode ~= "jpeg" then
        error("preview tasks support only jpeg output")
    end
    if task.task_type == "enumerate_presets" then
        if task.photo_path ~= "" then error("enumerate_presets photo_path must be empty") end
    elseif type(task.photo_path) ~= "string"
        or (not task.photo_path:match("^%a:[/\\]") and not task.photo_path:match("^[/\\][/\\]")) then
        error("photo_path must be an absolute Windows path")
    end
    if task.task_type ~= "enumerate_presets" then
        local extension = raw_extension(task.photo_path)
        if not SIDECAR_RAW[extension] then
            error("unsupported camera RAW extension for Lightroom XMP: " .. tostring(extension))
        end
    end
    if type(task.rating) ~= "number" or task.rating < 0 or task.rating > 5 or task.rating ~= math.floor(task.rating) then
        error("rating must be an integer from 0 to 5")
    end
    for _, key in ipairs({ "auto_tone", "auto_white_balance", "lens_profile", "remove_ca" }) do
        if type(task[key]) ~= "boolean" then error(key .. " must be boolean") end
    end
    if task.preset_uuid ~= nil then
        if type(task.preset_uuid) ~= "string" or not valid_identifier(task.preset_uuid) then
            error("invalid preset_uuid")
        end
        if task.preset_scope ~= "catalog" and task.preset_scope ~= "plugin" then
            error("preset_scope must be catalog or plugin")
        end
    end
    if type(task.preset_amount) ~= "number" or task.preset_amount < 0
        or task.preset_amount > 200 or task.preset_amount ~= math.floor(task.preset_amount) then
        error("preset_amount must be an integer from 0 to 200")
    end
    local look_field_count = 0
    for _, key in ipairs({ "look_descriptor_hash", "look_uuid" }) do
        if task[key] ~= nil then look_field_count = look_field_count + 1 end
    end
    if (look_field_count ~= 0 and look_field_count ~= 2)
        or (legacy_look_descriptor_path ~= nil and look_field_count ~= 2) then
        error("look_descriptor_hash and look_uuid must be provided together")
    end
    if look_field_count == 2 then
        if task.preset_uuid ~= nil then
            error("Creative Look descriptors cannot be combined with preset_uuid")
        end
        if not valid_sha256(task.look_descriptor_hash) then
            error("look_descriptor_hash must be a SHA-256 digest")
        end
        task.look_descriptor_hash = task.look_descriptor_hash:lower()
        if not valid_identifier(task.look_uuid) then error("invalid look_uuid") end
    elseif task.look_amount ~= 100 then
        error("look_amount requires a Creative Look descriptor")
    end
    if type(task.look_amount) ~= "number" or task.look_amount < 0
        or task.look_amount > 200 or task.look_amount ~= math.floor(task.look_amount) then
        error("look_amount must be an integer from 0 to 200")
    end
    if task.task_type == "enumerate_presets" and (
        task.preset_uuid ~= nil or task.look_descriptor_hash ~= nil
        or task.jpeg_output_dir ~= nil) then
        error("enumerate_presets does not accept preset, Look, or JPEG output settings")
    end
    if task.jpeg_output_dir ~= nil and (
        type(task.jpeg_output_dir) ~= "string"
        or (not task.jpeg_output_dir:match("^%a:[/\\]") and not task.jpeg_output_dir:match("^[/\\][/\\]"))
    ) then
        error("jpeg_output_dir must be an absolute Windows path")
    end
    for key, value in pairs(fields) do
        if key:sub(1, 5) == "crop." then
            local crop_key = key:sub(6)
            if crop_key ~= "CropLeft" and crop_key ~= "CropTop" and crop_key ~= "CropRight"
                and crop_key ~= "CropBottom" and crop_key ~= "CropAngle" then
                error("unsupported crop field: " .. crop_key)
            end
            if type(value) ~= "number" then error("crop fields must be numeric") end
            task.crop[crop_key] = value
        elseif key:sub(1, 6) == "style." then
            local style_key = key:sub(7)
            if not style_key:match("^[A-Za-z][A-Za-z0-9_]*$") or #style_key > 80 then
                error("invalid style field")
            end
            local value_type = type(value)
            if value_type ~= "boolean" and value_type ~= "number" and value_type ~= "string" then
                error("invalid style value")
            end
            task.style[style_key] = value
        elseif key ~= "batch_id" and key ~= "task_id" and key ~= "photo_path"
            and key ~= "rating" and key ~= "auto_tone" and key ~= "auto_white_balance"
            and key ~= "lens_profile" and key ~= "remove_ca" and key ~= "task_type"
            and key ~= "output_mode" and key ~= "preset_uuid" and key ~= "preset_scope"
            and key ~= "preset_amount" and key ~= "look_descriptor_path"
            and key ~= "look_descriptor_hash" and key ~= "look_uuid"
            and key ~= "look_amount" and key ~= "jpeg_output_dir"
            and key ~= "source_batch_id" and key ~= "source_task_id" then
            error("unsupported task field: " .. key)
        end
    end
    local left, top = task.crop.CropLeft, task.crop.CropTop
    local right, bottom = task.crop.CropRight, task.crop.CropBottom
    if left ~= nil or top ~= nil or right ~= nil or bottom ~= nil then
        if left == nil or top == nil or right == nil or bottom == nil then
            error("crop bounds must include left, top, right, and bottom")
        end
        if not (0 <= left and left < right and right <= 1 and 0 <= top and top < bottom and bottom <= 1) then
            error("crop bounds must be ordered between 0 and 1")
        end
    end
    if task.crop.CropAngle and (task.crop.CropAngle < -45 or task.crop.CropAngle > 45) then
        error("crop angle must be between -45 and 45")
    end
    if task.task_type == "cleanup_transient_snapshot" then
        if not valid_identifier(task.source_batch_id)
            or not valid_identifier(task.source_task_id) then
            error("cleanup_transient_snapshot requires valid source identifiers")
        end
        if task.source_batch_id:sub(1, 7) ~= "export-"
            or task.source_task_id:sub(1, 6) ~= "photo-" then
            error("cleanup_transient_snapshot may target only export-* / photo-* snapshots")
        end
    elseif task.source_batch_id ~= nil or task.source_task_id ~= nil then
        error("source snapshot identifiers require cleanup_transient_snapshot")
    end
    if task.task_type == "repair_preview_metadata"
        or task.task_type == "cleanup_transient_snapshot" then
        if task.output_mode ~= "xmp" then
            error(task.task_type .. " tasks support only xmp output")
        end
        if task.rating ~= 0 or task.auto_tone or task.auto_white_balance
            or task.lens_profile or task.remove_ca then
            error(task.task_type .. " tasks cannot apply ratings or develop settings")
        end
        if next(task.crop) ~= nil or next(task.style) ~= nil or task.preset_uuid ~= nil
            or task.preset_scope ~= "catalog" or task.preset_amount ~= 100
            or task.look_descriptor_hash ~= nil
            or task.look_uuid ~= nil or task.look_amount ~= 100
            or task.jpeg_output_dir ~= nil then
            error(task.task_type .. " tasks cannot apply crop, style, preset, Look, or JPEG settings")
        end
    end
    return task
end

function Bridge:terminal_result_path(stem)
    for _, directory in ipairs({ self.paths.done, self.paths.failed, self.paths.cancelled }) do
        local candidate = LrPathUtils.child(directory, stem .. ".result")
        if LrFileUtils.exists(candidate) then return candidate end
    end
    return nil
end

function Bridge:finish(source_path, task, status, message, details)
    local stem = task_name(source_path)
    if self:terminal_result_path(stem) then
        delete_if_exists(source_path)
        return
    end
    local target_directory = self.paths.failed
    if status == "done" then
        target_directory = self.paths.done
    elseif status == "cancelled" then
        target_directory = self.paths.cancelled
    end
    local fields = {
        batch_id = task and task.batch_id or "unknown",
        task_id = task and task.task_id or stem,
        photo_path = task and task.photo_path or "",
        status = status,
        finished_at = now_utc(),
        message = message or "",
    }
    for key, value in pairs(details or {}) do fields[key] = value end
    atomic_write(LrPathUtils.child(target_directory, stem .. ".result"), result_line(fields))
    delete_if_exists(source_path)
end

function Bridge:rollback(context)
    if not context or not context.modified or not context.photo then
        return "rollback=not-needed", true
    end
    local photo = context.photo
    local task = context.task
    local catalog = LrApplication.activeCatalog()
    local restored = false
    local method = nil
    local failures = {}
    local notes = {}
    local snapshot_restored = false

    if context.snapshot_id then
        local snapshot_ok, snapshot_error = LrTasks.pcall(function()
            self:assert_photo_identity(photo, task.photo_path, "snapshot rollback")
            catalog:withWriteAccessDo("照片选片 · 失败回滚快照", function()
                photo:applyDevelopSnapshot(context.snapshot_id)
                if task.task_type ~= "preview" then
                    photo:setRawMetadata("rating", context.original_rating)
                end
            end)
        end)
        if snapshot_ok then
            restored = true
            snapshot_restored = true
            method = "snapshot"
        else
            table.insert(failures, "snapshot=" .. safe_message(snapshot_error))
        end
    end

    -- Reapply the captured settings even after a successful snapshot. This
    -- covers Lightroom builds where applyDevelopSnapshot() returns without an
    -- error outside Develop but does not actually switch the settings.
    if context.original_settings then
        local settings_ok, settings_error = LrTasks.pcall(function()
            self:assert_photo_identity(photo, task.photo_path, "settings rollback")
            catalog:withWriteAccessDo("照片选片 · 失败回滚设置", function()
                photo:applyDevelopSettings(context.original_settings, "照片选片 · 失败回滚", true)
                if task.task_type ~= "preview" then
                    photo:setRawMetadata("rating", context.original_rating)
                end
            end)
        end)
        if settings_ok then
            restored = true
            method = snapshot_restored and "snapshot+develop-settings" or "develop-settings"
        else
            table.insert(failures, "settings=" .. safe_message(settings_error))
        end
    end
    if not restored then return "rollback=failed; " .. table.concat(failures, "; "), false end

    local snapshot_deleted, snapshot_delete_error = self:delete_transient_snapshot(photo, context)
    if snapshot_deleted then
        if context.transient then table.insert(notes, "temporary_snapshot=deleted") end
    else
        table.insert(failures, "temporary_snapshot_cleanup_failed=" .. snapshot_delete_error)
    end

    -- Let Lightroom flush the restored catalog settings first. If an XMP
    -- existed before the task, restore its exact bytes from the immutable bridge
    -- drive backup afterward and verify they remain stable.
    if not context.transient then
        local save_ok, save_error = LrTasks.pcall(function() photo:saveMetadata() end)
        if not save_ok then
            table.insert(failures, "rollback_xmp_save=" .. safe_message(save_error))
        else
            LrTasks.sleep(2.0)
        end
    end
    if context.pre_sidecar and context.pre_sidecar.exists and context.xmp_backup_path then
        local unchanged_ok, unchanged_or_error = LrTasks.pcall(function()
            return self:sidecar_is_unchanged(context)
        end)
        if unchanged_ok and unchanged_or_error then
            table.insert(notes, "xmp_unchanged=preserved-in-place")
        else
            local backup_ok, backup_or_error = LrTasks.pcall(function()
                self:restore_sidecar_backup(context)
            end)
            if backup_ok then
                table.insert(notes, "xmp_restore=byte-identical-backup")
            else
                local reason = unchanged_ok and "content-changed" or safe_message(unchanged_or_error)
                table.insert(failures, "xmp_restore_failed=" .. safe_message(backup_or_error)
                    .. "; unchanged_check=" .. reason)
            end
        end
    elseif context.pre_sidecar and not context.pre_sidecar.exists then
        if LrFileUtils.exists(context.sidecar_path) then
            -- Do not delete a newly appeared sidecar: another Lightroom/user
            -- action could have created it concurrently.  A transient task is
            -- therefore reported failed instead of claiming exact restoration.
            table.insert(failures, "xmp_originally_missing=unexpected-sidecar-retained-for-safety")
        else
            table.insert(notes, "xmp_originally_missing=still-missing")
        end
    end

    local message = "rollback=restored-from-" .. method
    if #notes > 0 then message = message .. "; " .. table.concat(notes, "; ") end
    if #failures > 0 then message = message .. "; " .. table.concat(failures, "; ") end
    return message, #failures == 0
end

function Bridge:process_isolated_preview(task, context)
    context.preview_isolated = true
    context.result.isolation_kind = "virtual_copy"
    context.result.isolation_status = "creating"
    context.result.restore_status = "running"
    local master = self:find_existing_master(task, context)
    context.master_photo = master
    context.result.source_uuid = context.source_uuid
    local working = self:create_preview_virtual_copy(master, task, context)
    context.result.working_uuid = context.working_uuid
    context.result.isolation_status = "active"
    self:apply_develop_settings(working, task, context)
    context.result.jpeg_status = "running"
    context.result.jpeg_path = self:export_jpeg(working, task, context, true)
    context.result.jpeg_status = "done"
    local removed, cleanup_message = self:remove_preview_virtual_copy(context)
    context.preview_cleanup_succeeded = removed
    context.preview_cleanup_message = cleanup_message
    context.result.isolation_status = context.preview_copy_removed and "removed" or "cleanup_failed"
    context.result.restore_status = removed and "done" or "failed"
    if not removed then
        error("isolated Lightroom preview cleanup failed: " .. cleanup_message)
    end
end

function Bridge:process(running_path)
    local context = { modified = false, imported = false }
    local task = nil
    local ok, err = LrTasks.pcall(function()
        task = self:parse_task(running_path)
        context.task = task
        context.result = {
            task_type = task.task_type,
            output_mode = task.output_mode,
            xmp_status = (task.output_mode == "xmp" or task.output_mode == "both") and "pending" or "not_requested",
            jpeg_status = (task.output_mode == "jpeg" or task.output_mode == "both") and "pending" or "not_requested",
            restore_status = "not_required",
            preset_status = task.preset_uuid and "pending" or "not_requested",
            preset_uuid = task.preset_uuid,
            preset_scope = task.preset_uuid and task.preset_scope or nil,
            preset_amount = task.preset_uuid and task.preset_amount or nil,
            look_status = task.look_descriptor_hash and "pending" or "not_requested",
            look_uuid = task.look_descriptor_hash and task.look_uuid or nil,
            look_amount = task.look_descriptor_hash and task.look_amount or nil,
        }
        self:ensure_not_cancelled(task, "before processing")
        if task.task_type == "enumerate_presets" then
            context.result.output_mode = nil
            context.result.xmp_status = "not_requested"
            context.result.jpeg_status = "not_requested"
            context.result.preset_status = "running"
            local listing_path, count = self:enumerate_presets(task, running_path)
            context.result.preset_status = "done"
            context.result.preset_list_path = listing_path
            context.result.preset_count = count
            return
        end
        if task.task_type == "repair_preview_metadata" then
            self:repair_preview_metadata(task, context)
            return
        end
        if task.task_type == "cleanup_transient_snapshot" then
            context.result.xmp_status = "not_requested"
            context.result.jpeg_status = "not_requested"
            self:cleanup_transient_snapshot(task, context)
            return
        end
        if task.task_type == "preview" then
            self:process_isolated_preview(task, context)
            return
        end
        context.sidecar_path = LrPathUtils.replaceExtension(task.photo_path, "xmp")
        self:backup_sidecar(task, context)
        local photo = self:find_or_import(task, context)
        context.photo = photo
        self:assert_photo_identity(photo, task.photo_path, "before backup")
        context.original_rating = photo:getRawMetadata("rating")
        context.original_settings = photo:getDevelopSettings()
        self:assert_photo_identity(photo, task.photo_path, "after backup")
        self:ensure_not_cancelled(task, "after backup")
        self:create_snapshot(photo, task, context)
        context.transient = task.output_mode == "jpeg"
        self:apply_develop_settings(photo, task, context)
        if task.output_mode == "xmp" or task.output_mode == "both" then
            context.result.xmp_status = "running"
            self:save_xmp(photo, task, context)
            context.result.xmp_status = "done"
            context.result.xmp_path = context.sidecar_path
            context.xmp_committed = true
        end
        if task.output_mode == "jpeg" or task.output_mode == "both" then
            context.result.jpeg_status = "running"
            context.result.jpeg_path = self:export_jpeg(photo, task, context, false)
            context.result.jpeg_status = "done"
        end
        if context.transient then
            context.result.restore_status = "running"
            local rollback_message, restored = self:rollback(context)
            context.rollback_attempted = true
            context.rollback_message = rollback_message
            context.result.restore_status = restored and "done" or "failed"
            if not restored then error("temporary Lightroom state restore failed: " .. rollback_message) end
        end
    end)
    if ok then
        local messages = {
            context.preview_isolated and "target=isolated-virtual-copy" or "target=object-addressed",
        }
        if context.snapshot_name then table.insert(messages, "snapshot=" .. tostring(context.snapshot_name)) end
        if context.xmp_state_path then table.insert(messages, "xmp_backup_state=" .. tostring(context.xmp_state_path)) end
        if context.xmp_verified then
            table.insert(messages, "xmp_saved_and_stable=true")
        end
        if context.result and context.result.jpeg_path then
            table.insert(messages, "jpeg=" .. tostring(context.result.jpeg_path))
        end
        if context.rollback_message then table.insert(messages, context.rollback_message) end
        if context.preview_cleanup_message then table.insert(messages, context.preview_cleanup_message) end
        if context.result and context.result.preset_list_path then
            table.insert(messages, "preset_listing=" .. tostring(context.result.preset_list_path))
        end
        local success_message = table.concat(messages, "; ")
        self:finish(running_path, task, "done", success_message, context.result)
        self:log("INFO", "completed " .. (task and task.photo_path or running_path))
    else
        local cancelled = tostring(err):find(CANCEL_ERROR, 1, true) ~= nil
        local incomplete_status = cancelled and "cancelled" or "failed"
        if context.result then
            if context.result.xmp_status == "running" or context.result.xmp_status == "pending" then
                context.result.xmp_status = incomplete_status
            end
            if context.result.jpeg_status == "running" or context.result.jpeg_status == "pending" then
                context.result.jpeg_status = incomplete_status
            end
            if context.result.preset_status == "running" or context.result.preset_status == "pending" then
                context.result.preset_status = incomplete_status
            end
            if context.result.look_status == "running" or context.result.look_status == "pending" then
                context.result.look_status = incomplete_status
            end
        end
        local rollback_message = context.rollback_message
        if context.preview_isolated then
            local cleanup_ok = context.preview_cleanup_succeeded
            if not context.preview_copy_removed and (
                context.preview_intent_path or context.preview_started_path
                or context.preview_state_path or context.working_uuid
            ) then
                local cleanup_call_ok, removed_or_error, cleanup_message = LrTasks.pcall(function()
                    return self:remove_preview_virtual_copy(context)
                end)
                if cleanup_call_ok then
                    cleanup_ok = removed_or_error
                    rollback_message = cleanup_message
                else
                    cleanup_ok = false
                    rollback_message = "preview_isolation=cleanup-exception; "
                        .. safe_message(removed_or_error)
                end
                context.preview_cleanup_succeeded = cleanup_ok
                context.preview_cleanup_message = rollback_message
            else
                rollback_message = context.preview_cleanup_message or "preview_isolation=not-created"
            end
            if context.result then
                if context.preview_copy_removed then
                    context.result.isolation_status = "removed"
                    context.result.restore_status = cleanup_ok and "done" or "failed"
                elseif context.preview_creation_started then
                    context.result.isolation_status = "cleanup_failed"
                    context.result.restore_status = "failed"
                else
                    context.result.isolation_status = "not_created"
                    context.result.restore_status = "not_required"
                end
            end
        else
            local preserve_committed_xmp = task and task.output_mode == "both" and context.xmp_committed
            if preserve_committed_xmp then
                rollback_message = "rollback=not-run-after-successful-xmp-commit"
            elseif not context.rollback_attempted then
                local restored
                rollback_message, restored = self:rollback(context)
                if context.result and context.modified then
                    context.result.restore_status = restored and "done" or "failed"
                end
            end
        end
        rollback_message = rollback_message or "rollback=not-needed"
        local message = safe_message(err) .. "; " .. rollback_message
        if context.snapshot_name then message = message .. "; safety_snapshot=" .. context.snapshot_name end
        if context.imported then message = message .. "; imported_into_catalog=true" end
        self:finish(running_path, task, cancelled and "cancelled" or "failed", message, context.result)
        self:log(cancelled and "INFO" or "ERROR", message)
    end
end

function Bridge:recover_active_preview_copy(path)
    local record = self:read_preview_copy_record(path, "active")
    local catalog = LrApplication.activeCatalog()
    if path_key(catalog:getPath()) ~= path_key(record.catalog_path) then
        self:log("ERROR", "preview orphan belongs to another catalog; retained: " .. path)
        return false
    end
    local task = {
        batch_id = record.batch_id,
        task_id = record.task_id,
        photo_path = record.photo_path,
        task_type = "preview",
    }
    local master = catalog:findPhotoByUuid(record.source_uuid)
    local by_path = catalog:findPhotoByPath(record.photo_path)
    if not master or not by_path or not same_photo(master, by_path) then
        error("preview orphan master identity no longer resolves exactly")
    end
    local context = {
        task = task,
        preview_isolated = true,
        preview_catalog = catalog,
        preview_catalog_path = record.catalog_path,
        preview_record = record,
        preview_state_path = path,
        preview_copy_name = record.copy_name,
        preview_creation_started = true,
        source_uuid = record.source_uuid,
        source_local_id = record.source_local_id,
        working_uuid = record.working_uuid,
        working_local_id = record.working_local_id,
        master_photo = master,
    }
    self:assert_master_identity(master, task, context, "preview orphan recovery")
    local recovery_selection, missing_selection = self:selection_from_record(catalog, record, true)
    self:assert_preview_catalog_identity(context, "preview orphan selection reconstruction")
    context.selection_restore_override = recovery_selection
    local removed, message = self:remove_preview_virtual_copy(context)
    if not removed then error(message) end
    if #missing_selection > 0 then
        error("isolated preview copy was removed, but " .. tostring(#missing_selection)
            .. " saved selection member(s) no longer exist; remaining selection restored")
    end
    self:log("INFO", "recovered isolated preview virtual copy: " .. record.batch_id
        .. "--" .. record.task_id .. "; " .. message)
    return true
end

function Bridge:recover_started_preview_copy(path)
    local record = self:read_preview_copy_record(path, "started")
    local catalog = LrApplication.activeCatalog()
    if path_key(catalog:getPath()) ~= path_key(record.catalog_path) then
        self:log("ERROR", "preview started record belongs to another catalog; retained: " .. path)
        return false
    end
    local paths = self:preview_copy_record_paths(record.batch_id, record.task_id)
    if LrFileUtils.exists(paths.removed) then
        local removed_record = self:read_preview_copy_record(paths.removed, "removed")
        self:assert_preview_record_lineage(
            removed_record,
            record,
            "started/removed preview recovery convergence"
        )
        self:clear_preview_selection_restore_attempt(
            paths,
            record,
            "started/selection-attempt preview recovery convergence"
        )
        delete_if_exists(path)
        self:log("INFO", "cleared redundant preview started record after verified removal: "
            .. record.batch_id .. "--" .. record.task_id)
        return true
    end
    if LrFileUtils.exists(paths.active) then
        local active_record = self:read_preview_copy_record(paths.active, "active")
        self:assert_preview_record_lineage(
            active_record,
            record,
            "started/active preview recovery convergence"
        )
        error("preview active state still requires exact UUID cleanup; started record retained")
    end
    local master = catalog:findPhotoByUuid(record.source_uuid)
    local by_path = catalog:findPhotoByPath(record.photo_path)
    if not master or not by_path or not same_photo(master, by_path) then
        error("preview started record master identity no longer resolves exactly; retained")
    end
    local task = {
        batch_id = record.batch_id,
        task_id = record.task_id,
        photo_path = record.photo_path,
        task_type = "preview",
    }
    local context = {
        task = task,
        preview_isolated = true,
        preview_catalog = catalog,
        preview_catalog_path = record.catalog_path,
        preview_record = record,
        preview_started_path = path,
        preview_copy_name = record.copy_name,
        preview_creation_started = true,
        source_uuid = record.source_uuid,
        source_local_id = record.source_local_id,
        master_photo = master,
    }
    self:assert_master_identity(master, task, context, "preview started-record recovery")
    local attempted, missing = self:restore_ambiguous_preview_selection_once(
        context,
        catalog,
        paths,
        record,
        "preview started-record selection restore"
    )
    local selection_note = attempted
        and "one-time selection restore attempted"
        or "prior selection-restore attempt marker found; selection not changed again"
    if #missing > 0 then
        selection_note = selection_note .. "; " .. tostring(#missing)
            .. " saved selection member(s) no longer exist"
    end
    error("preview creation may have started before its exact working UUID was durable; "
        .. "started record retained for safe diagnosis; " .. selection_note)
end

function Bridge:recover_preview_copy_intent(path)
    local record = self:read_preview_copy_record(path, "intent")
    local catalog = LrApplication.activeCatalog()
    if path_key(catalog:getPath()) ~= path_key(record.catalog_path) then
        self:log("ERROR", "preview intent belongs to another catalog; retained: " .. path)
        return false
    end
    local paths = self:preview_copy_record_paths(record.batch_id, record.task_id)
    for _, candidate in ipairs({
        { path = paths.removed, phase = "removed", label = "removed" },
        { path = paths.active, phase = "active", label = "active" },
        { path = paths.started, phase = "started", label = "started" },
    }) do
        if LrFileUtils.exists(candidate.path) then
            local stronger_record = self:read_preview_copy_record(candidate.path, candidate.phase)
            self:assert_preview_record_lineage(
                stronger_record,
                record,
                "intent/" .. candidate.label .. " preview recovery convergence"
            )
            if candidate.phase == "removed" then
                self:clear_preview_selection_restore_attempt(
                    paths,
                    record,
                    "intent/selection-attempt preview recovery convergence"
                )
            end
            delete_if_exists(path)
            self:log("INFO", "cleared redundant preview intent after verified "
                .. candidate.label .. " record: " .. record.batch_id .. "--" .. record.task_id)
            return true
        end
    end
    local master = catalog:findPhotoByUuid(record.source_uuid)
    local by_path = catalog:findPhotoByPath(record.photo_path)
    if not master or not by_path or not same_photo(master, by_path) then
        error("preview intent master identity no longer resolves exactly")
    end
    local task = {
        batch_id = record.batch_id,
        task_id = record.task_id,
        photo_path = record.photo_path,
        task_type = "preview",
    }
    local context = {
        task = task,
        preview_isolated = true,
        preview_catalog = catalog,
        preview_catalog_path = record.catalog_path,
        preview_record = record,
        preview_intent_path = path,
        preview_copy_name = record.copy_name,
        source_uuid = record.source_uuid,
        source_local_id = record.source_local_id,
        master_photo = master,
    }
    self:assert_master_identity(master, task, context, "preview intent recovery")
    if record.creation_started then
        -- Legacy protocol /1 intents did not durably distinguish pre-create
        -- from creation-in-progress. Never infer absence from a mutable copy
        -- name; retain the record because only a Lightroom UUID is destructive-safe.
        local attempted, missing = self:restore_ambiguous_preview_selection_once(
            context,
            catalog,
            paths,
            record,
            "legacy preview intent selection restore"
        )
        local selection_note = attempted
            and "one-time selection restore attempted"
            or "prior selection-restore attempt marker found; selection not changed again"
        if #missing > 0 then
            selection_note = selection_note .. "; " .. tostring(#missing)
                .. " saved selection member(s) no longer exist"
        end
        error("legacy preview intent may have started creation but has no verified working UUID; "
            .. "retained; " .. selection_note)
    end
    local recorded_selection, missing = self:selection_from_record(catalog, record, true)
    context.selection_restore_override = recorded_selection
    local restored, restore_error = self:restore_saved_preview_selection(
        context,
        catalog,
        "preview intent recovery restore"
    )
    if not restored then
        error("could not restore selection from preview intent: " .. restore_error)
    end
    self:archive_preview_copy_record(context, path)
    context.preview_copy_removed = true
    if #missing > 0 then
        self:log("ERROR", "cleared pre-create preview intent, but " .. tostring(#missing)
            .. " saved selection member(s) no longer exist")
    end
    self:log("INFO", "cleared preview intent with no virtual copy: "
        .. record.batch_id .. "--" .. record.task_id)
    return true
end

function Bridge:recover_preview_selection_attempt(path)
    local record = self:read_preview_copy_record(path, "selection_restore_attempted")
    local paths = self:preview_copy_record_paths(record.batch_id, record.task_id)
    if LrFileUtils.exists(paths.removed) then
        local removed_record = self:read_preview_copy_record(paths.removed, "removed")
        self:assert_preview_record_lineage(
            removed_record,
            record,
            "orphan selection-attempt/removed convergence"
        )
        delete_if_exists(path)
        return true
    end
    for _, candidate in ipairs({
        { path = paths.active, phase = "active" },
        { path = paths.started, phase = "started" },
        { path = paths.intent, phase = "intent" },
    }) do
        if LrFileUtils.exists(candidate.path) then
            local owner = self:read_preview_copy_record(candidate.path, candidate.phase)
            self:assert_preview_record_lineage(
                owner,
                record,
                "selection-attempt owner convergence"
            )
            self:log("ERROR", "preview selection restore was already attempted once; "
                .. "marker retained without changing selection again: " .. path)
            return false
        end
    end
    error("preview selection-restore attempt marker has no verified owner; retained")
end

function Bridge:recover_preview_copy_states()
    for _, path in ipairs(collect_suffix_entries(self.paths.backups, ".preview-copy.state")) do
        local recovered, recovery_error = LrTasks.pcall(function()
            self:recover_active_preview_copy(path)
        end)
        if not recovered then
            self:log("ERROR", "could not recover isolated preview state " .. path
                .. ": " .. safe_message(recovery_error))
        end
    end
    for _, path in ipairs(collect_suffix_entries(self.paths.backups, ".preview-copy.started")) do
        local recovered, recovery_error = LrTasks.pcall(function()
            self:recover_started_preview_copy(path)
        end)
        if not recovered then
            self:log("ERROR", "could not resolve isolated preview started record " .. path
                .. ": " .. safe_message(recovery_error))
        end
    end
    for _, path in ipairs(collect_suffix_entries(self.paths.backups, ".preview-copy.intent")) do
        local recovered, recovery_error = LrTasks.pcall(function()
            self:recover_preview_copy_intent(path)
        end)
        if not recovered then
            self:log("ERROR", "could not resolve isolated preview intent " .. path
                .. ": " .. safe_message(recovery_error))
        end
    end
    for _, path in ipairs(collect_suffix_entries(
        self.paths.backups,
        ".preview-copy.selection-restore-attempted"
    )) do
        local recovered, recovery_error = LrTasks.pcall(function()
            self:recover_preview_selection_attempt(path)
        end)
        if not recovered then
            self:log("ERROR", "could not resolve preview selection-restore attempt " .. path
                .. ": " .. safe_message(recovery_error))
        end
    end
end

function Bridge:recover_running_tasks()
    self:recover_preview_copy_states()
    local entries = collect_task_entries(self.paths.running)
    for _, path in ipairs(entries) do
        local stem = task_name(path)
        if self:terminal_result_path(stem) then
            delete_if_exists(path)
        else
            local task = nil
            local parsed, parsed_or_error = pcall(function() return self:parse_task(path) end)
            if parsed then task = parsed_or_error end
            local detail = parsed and "" or ("; parse_error=" .. safe_message(parsed_or_error))
            local details = nil
            if task and task.task_type == "preview" then
                local paths = self:preview_copy_record_paths(task.batch_id, task.task_id)
                local isolation_status = "not_created"
                local restore_status = "not_required"
                if LrFileUtils.exists(paths.removed) then
                    isolation_status = "removed"
                    restore_status = "done"
                elseif LrFileUtils.exists(paths.active) or LrFileUtils.exists(paths.started)
                    or LrFileUtils.exists(paths.intent)
                    or LrFileUtils.exists(paths.selection_attempted) then
                    isolation_status = "cleanup_failed"
                    restore_status = "failed"
                end
                details = {
                    task_type = "preview",
                    output_mode = "jpeg",
                    xmp_status = "not_requested",
                    jpeg_status = "failed",
                    isolation_kind = "virtual_copy",
                    isolation_status = isolation_status,
                    restore_status = restore_status,
                }
            end
            local recovered, recovery_error = pcall(function()
                self:finish(
                    path,
                    task,
                    "failed",
                    "plugin restarted while task was running; previous Lightroom operation is indeterminate" .. detail,
                    details
                )
            end)
            if recovered then
                self:log("ERROR", "recovered stale running task as failed: " .. stem)
            else
                self:log("ERROR", "could not recover stale running task " .. stem .. ": " .. safe_message(recovery_error))
            end
        end
    end
end

function Bridge:poll_once()
    local entries = collect_task_entries(self.paths.pending)
    for _, path in ipairs(entries) do
        if not self.running then break end
        local task = nil
        local parsed, parsed_or_error = pcall(function() return self:parse_task(path) end)
        if parsed then task = parsed_or_error end
        if task and self:is_cancelled(task) then
            self:finish(path, task, "cancelled", "cancelled before claim; rollback=not-needed")
        else
            -- Cancellation is checked immediately before claim and again at the
            -- start of process(), closing the only queue-state race safely.
            local running_path = self:claim(path)
            if running_path then
                self:heartbeat("processing")
                self:process(running_path)
            elseif not parsed then
                self:log("ERROR", "could not claim invalid task: " .. safe_message(parsed_or_error))
            end
        end
    end
end

function Bridge:start()
    if self.running then return end
    self.running = true
    LrTasks.startAsyncTask(function()
        if not self:ensure_layout() then
            self.running = false
            return
        end
        self:log("INFO", "bridge started: " .. self.root)
        local recovered, recovery_error = LrTasks.pcall(function() self:recover_running_tasks() end)
        if not recovered then
            self:log("ERROR", "startup recovery failed: " .. safe_message(recovery_error))
        end
        local registered, registration_error = LrTasks.pcall(function()
            self:register_managed_presets(false)
        end)
        if not registered then
            self:log("ERROR", "managed preset registration failed: " .. safe_message(registration_error))
        end
        while self.running do
            local ok, err = LrTasks.pcall(function()
                self:heartbeat("running")
                self:refresh_managed_presets_if_due()
                self:poll_once()
            end)
            if not ok then self:log("ERROR", safe_message(err)) end
            if self.running then LrTasks.sleep(1.0) end
        end
        pcall(function() self:heartbeat("stopped") end)
        self:log("INFO", "bridge stopped")
    end)
end

function Bridge:stop()
    self.running = false
end

return Bridge
