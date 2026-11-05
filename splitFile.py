with open("names.txt", "r") as f:
    fullText = f.readlines()
    allNames = []
    allNumbers = []
    for line in fullText:
        line = line.strip().replace("\"", "").replace(",", "").split(":")
        allNames.append(line[0])
        allNumbers.append(line[1].replace(" ", ""))

with open("out.txt", "a") as f:
    for name in allNames:
        f.write(f"{name}\n")
    f.write(f"================================\n")
    for number in allNumbers:
        f.write(f"{number}\n")