import struct
import olefile

PATH = r"D:/WorkBuddy_space/Solidworks_AssetMate_MechDog/项目规划 - AssetMate_MechDog/_bom_solution_work/BOM_KIT/宏/insert_bom.swp"


def decompress(data):
    out = bytearray()
    pos = 0
    while pos + 2 <= len(data):
        header = struct.unpack_from('<H', data, pos)[0]
        pos += 2
        size = (header & 0x0FFF) + 3
        compressed = bool(header & 0x8000)
        end = min(pos + size, len(data))
        if not compressed:
            out += data[pos:end]
            pos = end
            continue
        chunk = data[pos:end]
        pos = end
        base = len(out)
        i = 0
        while i < len(chunk):
            flag = chunk[i]
            i += 1
            for bit in range(8):
                if i >= len(chunk):
                    break
                if not (flag >> bit) & 1:
                    out.append(chunk[i])
                    i += 1
                else:
                    if i + 2 > len(chunk):
                        break
                    token = struct.unpack_from('<H', chunk, i)[0]
                    i += 2
                    diff = len(out) - base
                    bits = max(4, (diff - 1).bit_length())
                    lmask = 0xFFFF >> bits
                    length = (token & lmask) + 3
                    offset = ((token & (~lmask & 0xFFFF)) >> (16 - bits)) + 1
                    src = len(out) - offset
                    for _ in range(length):
                        if 0 <= src < len(out):
                            out.append(out[src])
                            src += 1
    return bytes(out)


ole = olefile.OleFileIO(PATH)
for entry in ole.listdir():
    print('/'.join(entry))
raw = ole.openstream('apc/The VBA Project/_VBA_Project/VBA/Macro11').read()
text = decompress(raw[1:]).decode('latin1', errors='replace')
print("-----CODE-----")
print(text)
