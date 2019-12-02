import argparse
import gc
import pandas as pd
import torch.optim as optim
import torch.utils.data
from tqdm import tqdm
from data import get_training_set
import logger
from rbpn import Net as RBPN
from SRGAN.model import Discriminator
import torch.nn as nn
from torch.autograd import Variable
from torch.utils.data import DataLoader

################################################## iSEEBETTER TRAINER KNOBS #############################################
UPSCALE_FACTOR = 4
########################################################################################################################

# Handle command line arguments
parser = argparse.ArgumentParser(description='Train iSeeBetter: Super Resolution Models')
parser.add_argument('--upscale_factor', type=int, default=4, help="super resolution upscale factor")
parser.add_argument('--batchSize', type=int, default=2, help='training batch size')
parser.add_argument('--testBatchSize', type=int, default=5, help='testing batch size')
parser.add_argument('--start_epoch', type=int, default=1, help='Starting epoch for continuing training')
parser.add_argument('--nEpochs', type=int, default=150, help='number of epochs to train for')
parser.add_argument('--snapshots', type=int, default=1, help='Snapshots')
parser.add_argument('--lr', type=float, default=1e-4, help='Learning Rate. Default=0.01')
parser.add_argument('--gpu_mode', type=bool, default=False)
parser.add_argument('--threads', type=int, default=8, help='number of threads for data loader to use')
parser.add_argument('--seed', type=int, default=123, help='random seed to use. Default=123')
parser.add_argument('--gpus', default=8, type=int, help='number of gpu')
parser.add_argument('--data_dir', type=str, default='./vimeo_septuplet/sequences')
parser.add_argument('--file_list', type=str, default='sep_trainlist.txt')
parser.add_argument('--other_dataset', type=bool, default=False, help="use other dataset than vimeo-90k")
parser.add_argument('--future_frame', type=bool, default=True, help="use future frame")
parser.add_argument('--nFrames', type=int, default=7)
parser.add_argument('--patch_size', type=int, default=64, help='0 to use original frame size')
parser.add_argument('--data_augmentation', type=bool, default=True)
parser.add_argument('--model_type', type=str, default='RBPN')
parser.add_argument('--residual', type=bool, default=False)
parser.add_argument('--pretrained_sr', default='3x_dl10VDBPNF7_epoch_84.pth', help='sr pretrained base model')
parser.add_argument('--pretrained', type=bool, default=False)
parser.add_argument('--save_folder', default='weights/', help='Location to save checkpoint models')
parser.add_argument('--prefix', default='F7', help='Location to save checkpoint models')
parser.add_argument('-v', '--debug', default=False, action='store_true', help='Print debug spew.')

args = parser.parse_args()

# Load dataset
print('===> Loading datasets')
train_set = get_training_set(args.data_dir, args.nFrames, args.upscale_factor, args.data_augmentation, args.file_list,
                             args.other_dataset, args.patch_size, args.future_frame)
training_data_loader = DataLoader(dataset=train_set, num_workers=args.threads, batch_size=args.batchSize, shuffle=True)

# Initialize Logger
logger.initLogger(args.debug)

# Use Generator as RBPN
netG = RBPN(num_channels=3, base_filter=256,  feat = 64, num_stages=3, n_resblock=5, nFrames=args.nFrames, scale_factor=args.upscale_factor)
print('# of Generator parameters:', sum(param.numel() for param in netG.parameters()))

# Use Discriminator from SRGAN
netD = Discriminator()
print('# of Discriminator parameters:', sum(param.numel() for param in netD.parameters()))

#generatorCriterion = GeneratorLoss()
generatorCriterion = nn.L1Loss()

if torch.cuda.is_available():
    def printCUDAStats():
        logger.info("# of CUDA devices detected: %s", torch.cuda.device_count())
        logger.info("Using CUDA device #: %s", torch.cuda.current_device())
        logger.info("CUDA device name: %s", torch.cuda.get_device_name(torch.cuda.current_device()))

    printCUDAStats()

    netG.cuda()
    netD.cuda()
    generatorCriterion.cuda()

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# Use Adam optimizer
optimizerG = optim.Adam(netG.parameters(), lr=args.lr, betas=(0.9, 0.999), eps=1e-8)
optimizerD = optim.Adam(netD.parameters(), lr=args.lr, betas=(0.9, 0.999), eps=1e-8)

results = {'DLoss': [], 'GLoss': [], 'DScore': [], 'GScore': [], 'PSNR': [], 'SSIM': []}

def trainModel(epoch):
    trainBar = tqdm(training_data_loader)
    runningResults = {'batchSize': 0, 'DLoss': 0, 'GLoss': 0, 'DScore': 0, 'GScore': 0}

    netG.train()
    netD.train()

    # Skip first iteration
    iterTrainBar = iter(trainBar)
    next(iterTrainBar)

    for data in iterTrainBar:
        runningResults['batchSize'] += args.batchSize

        ################################################################################################################
        # (1) Update D network: maximize D(x)-1-D(G(z))
        ################################################################################################################
        # fakeHRs = []
        # fakeLRs = []
        fakeScrs = []
        realScrs = []
        DLoss = 0

        # Zero-out gradients, i.e., start afresh
        netD.zero_grad()

        #import pdb; pdb.set_trace()
        input, target, neigbor, flow, bicubic = data[0], data[1], data[2], data[3], data[4]
        input = Variable(input).to(device=device, dtype=torch.float)
        bicubic = Variable(bicubic).to(device=device, dtype=torch.float)
        neigbor = [Variable(j).to(device=device, dtype=torch.float) for j in neigbor]
        flow = [Variable(j).to(device=device, dtype=torch.float) for j in flow]

        #HRImg = HRImg.to(device)
        #LRImg = LRImg.to(device)

        fakeHR = netG(input, neigbor, flow)
        if args.residual:
            fakeHR = fakeHR + bicubic

        #import pdb; pdb.set_trace()
        realOut = netD(target).mean()
        fake_out = netD(fakeHR).mean()

        # fakeHRs.append(fakeHR)
        # fakeLRs.append(fakeLR)
        # fakeScrs.append(fake_out)
        # realScrs.append(realOut)

        DLoss += 1 - realOut + fake_out

        DLoss /= len(data)

        # Calculate gradients
        DLoss.backward(retain_graph=True)

        # Update weights
        optimizerD.step()

        ################################################################################################################
        # (2) Update G network: minimize 1-D(G(z)) + Perception Loss + Image Loss + TV Loss
        ################################################################################################################
        GLoss = 0

        # Zero-out gradients, i.e., start afresh
        netG.zero_grad()

        #idx = 0
        # for fakeHR, fakeLR, fake_scr, HRImg, LRImg in zip(fakeHRs, fakeLRs, fakeScrs, target, data):
        #     fakeHR = fakeHR.to(device)
        #     fakeLR = fakeLR.to(device)
        #     fake_scr = fake_scr.to(device)
        #     HRImg = HRImg.to(device)
        #     LRImg = LRImg.to(device)
        #     GLoss += generatorCriterion(fake_scr, fakeHR, HRImg, fakeLR, LRImg, idx)
        #     idx += 1

        GLoss = generatorCriterion(fakeHR, target)

        #import pdb; pdb.set_trace()
        GLoss /= len(data)

        # Calculate gradients
        GLoss.backward()

        # Update weights
        optimizerG.step()

        realOut = torch.Tensor(realScrs).mean()
        fake_out = torch.Tensor(fakeScrs).mean()
        runningResults['GLoss'] += GLoss.item() * args.batchSize
        runningResults['DLoss'] += DLoss.item() * args.batchSize
        runningResults['DScore'] += realOut.item() * args.batchSize
        runningResults['GScore'] += fake_out.item() * args.batchSize

        trainBar.set_description(desc='[Epoch: %d/%d] D Loss: %.4f G Loss: %.4f D(x): %.4f D(G(z)): %.4f' %
                                       (epoch, args.nEpochs, runningResults['DLoss'] / runningResults['batchSize'],
                                       runningResults['GLoss'] / runningResults['batchSize'],
                                       runningResults['DScore'] / runningResults['batchSize'],
                                       runningResults['GScore'] / runningResults['batchSize']))
        gc.collect()

    netG.eval()

    # learning rate is decayed by a factor of 10 every half of total epochs
    if (epoch + 1) % (args.nEpochs / 2) == 0:
        for param_group in optimizerG.param_groups:
            param_group['lr'] /= 10.0
        print('Learning rate decay: lr={}'.format(optimizerG.param_groups[0]['lr']))

    return runningResults

def saveModelParams(epoch, runningResults, validationResults={}):
    # Save model parameters
    torch.save(netG.state_dict(), 'weights/netG_epoch_%d_%d.pth' % (UPSCALE_FACTOR, epoch))
    torch.save(netD.state_dict(), 'weights/netD_epoch_%d_%d.pth' % (UPSCALE_FACTOR, epoch))

    logger.info("Checkpoint saved to {}".format('weights/netG_epoch_%d_%d.pth' % (UPSCALE_FACTOR, epoch)))

    # Save Loss\Scores\PSNR\SSIM
    results['DLoss'].append(runningResults['DLoss'] / runningResults['batchSize'])
    results['GLoss'].append(runningResults['GLoss'] / runningResults['batchSize'])
    results['DScore'].append(runningResults['DScore'] / runningResults['batchSize'])
    results['GScore'].append(runningResults['GScore'] / runningResults['batchSize'])
    #results['PSNR'].append(validationResults['PSNR'])
    #results['SSIM'].append(validationResults['SSIM'])

    if epoch % 1 == 0 and epoch != 0:
        out_path = 'statistics/'
        data_frame = pd.DataFrame(data={'DLoss': results['DLoss'], 'GLoss': results['GLoss'], 'DScore': results['DScore'],
                                  'GScore': results['GScore']},#, 'PSNR': results['PSNR'], 'SSIM': results['SSIM']},
                                  index=range(1, epoch + 1))
        data_frame.to_csv(out_path + 'iSeeBetter_' + str(UPSCALE_FACTOR) + '_Train_Results.csv', index_label='Epoch')

def main():
    """ Lets begin the training process! """

    for epoch in range(args.start_epoch, args.nEpochs + 1):
        runningResults = trainModel(epoch)

        if (epoch + 1) % (args.snapshots) == 0:
            saveModelParams(epoch, runningResults)

if __name__ == "__main__":
    main()