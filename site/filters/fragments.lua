-- Pandoc filter that embeds the Markdown files scripts/site_pages.py writes before each render.
--
-- A page cannot pull these files in with the include shortcode. Quarto expands that shortcode
-- while it scans the project, which is before the pre-render script has run, so on a clean
-- checkout the file does not exist yet and the whole render stops. This filter reads the file
-- later, when the script has written it. Files under docs/ are committed, so pages include
-- them with the shortcode as usual.
--
--   ::: {fragment="_generated/status.md"}
--   :::
--
-- is replaced by the blocks of that file. A path is relative to the project folder (site/).
--
--   ::: {fragments="_generated/gates.txt" wrap="demote" empty="Nothing here yet."}
--   :::
--
-- is replaced by the files that the list names, one path per line. Each file goes into a div
-- with the class given by `wrap`, which docs.lua then acts on. When the list is empty the
-- `empty` text is shown instead.

local function read(relative)
  local root = quarto.project.directory or "."
  local handle = io.open(root .. "/" .. relative, "r")
  if not handle then
    error("cannot read " .. relative .. ". Run scripts/site_pages.py first, or use make site.")
  end
  local text = handle:read("a")
  handle:close()
  return text
end

local function blocks_of(relative)
  return pandoc.read(read(relative), "markdown").blocks
end

function Div(div)
  local fragment = div.attributes["fragment"]
  if fragment then
    return blocks_of(fragment)
  end

  local list = div.attributes["fragments"]
  if list then
    local result = pandoc.Blocks({})
    for path in read(list):gmatch("[^\r\n]+") do
      local blocks = blocks_of(path)
      local wrap = div.attributes["wrap"]
      if wrap then
        blocks = pandoc.Blocks({ pandoc.Div(blocks, pandoc.Attr("", { wrap })) })
      end
      result:extend(blocks)
    end
    if #result == 0 and div.attributes["empty"] then
      return pandoc.read(div.attributes["empty"], "markdown").blocks
    end
    return result
  end
end
