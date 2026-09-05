-- Window Attention subsystem.
-- See ~/.local/share/window-attention/README.md for architecture and recovery.

-- Activation requests may mark a window as urgent, but may not interrupt the
-- user's current work by moving keyboard focus or switching workspaces.
hl.config({
  misc = {
    focus_on_activate = false,
  },
})

-- The window-attention service owns this static tag. Rendering stays
-- declarative here so the service does not contain theme or presentation code.
local function paired_border(gradient)
  local colors = {}
  for _, value in ipairs(gradient.colors) do
    local alpha, red, green, blue = value:match("^0x(%x%x)(%x%x)(%x%x)(%x%x)$")
    table.insert(colors, "rgba(" .. red .. green .. blue .. alpha .. ")")
  end
  -- Use the dedicated two-solid-color parser. In Hyprland 0.56.2 the
  -- general gradient parser initializes the active gradient's shader colors
  -- but omits that initialization for the inactive gradient.
  -- Attention uses the first theme stop as a solid whole-border pulse.
  return colors[1] .. " " .. colors[1]
end

local inactive_border = paired_border(hl.get_config("general.col.inactive_border"))
local active_border = paired_border(hl.get_config("general.col.active_border"))

o.window({ tag = "window-attention" }, {
  border_size = 5,
  border_color = inactive_border,
})

-- The service toggles this second static tag every 900ms. Hyprland's existing
-- `border` animation interpolates each transition, producing a whole-border
-- pulse between the current theme's inactive and active border colors.
o.window({ tag = "window-attention-pulse" }, {
  border_size = 5,
  border_color = active_border,
})
