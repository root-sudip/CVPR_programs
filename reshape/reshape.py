from PIL import Image
import matplotlib.pyplot as plt
import numpy as np
from os import listdir
import os

ARR = np.empty([1,4])

i = 0
k = 0

for f in listdir('data/'):
	img = Image.open('data/'+f)
	d = np.asarray(img)
	print('Shape : ',d.shape)

	txt_name = 'co/'+os.path.splitext(f)[0]+'.txt'

	fo = open(txt_name, "r")
	for line in fo:
		if not line.startswith('#'):
			for word in line.split():
				ARR[0][i] = int(word)
				print(int(word))
				i = i +1

		img2 = img.crop((int(ARR[0][0]), int(ARR[0][1]), int(ARR[0][0] + ARR[0][2]), int(ARR[0][1] + ARR[0][3])))
		name = "resized/new-img" + str(k) + ".png"
		A = np.asarray(img2)

		if(A.shape[1] < A.shape[0]):
			print('Image no : ',k)
			c = A.shape[0] / A.shape[1]
			w2 = 32 * c
			print('C : W2 : H2 || ',c,w2,32)
			resize = img2.resize((32,int(w2)),Image.ANTIALIAS)
			resize.save(name)
		else:
			print('Image no : ',k)
			c = A.shape[0] / A.shape[1]
			h2 = 32 / c
			print('C : W2 : H2 || ',c,32,h2)
			resize = img2.resize((int(h2),32),Image.ANTIALIAS)
			resize.save(name)
	#img2.save(name)
		k = k + 1
		i = 0