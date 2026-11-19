with open("names.txt", "r") as f:
    fullText = f.readlines()
    allNames = []
    allNumbers = []
    for line in fullText:
        line = line.strip().split("\t")
        print(line)
        allNames.append(line[0])
        allNumbers.append(line[1].replace(" ", ""))

with open("out.txt", "a") as f:
    for name, number in zip(allNames, allNumbers):
        f.write(f"\"{name}\": \"{number}\",\n")